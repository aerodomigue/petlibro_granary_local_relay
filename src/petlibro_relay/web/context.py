"""Read-only dependency container for the relay dashboard.

Every method here is a projection of state the relay already owns. Nothing in
this module - or anything it is reachable from - publishes MQTT, mutates the
registry, or writes to a database. The dashboard observes; it never controls.
"""

from __future__ import annotations

import os
import platform
import shutil
import threading
import time
from pathlib import Path
from typing import Any, cast

from ..config import RelayConfig
from ..device_context import LOCAL_TO_UPSTREAM, UPSTREAM_TO_LOCAL
from ..device_manager import DeviceManager
from ..device_registry import DeviceRegistry, DeviceRegistryEntry
from ..message_queue import MessageQueue
from ..observability.log_buffer import RingBufferLogHandler
from ..observability.sanitizer import mask_username, sanitize_value
from ..observability.telemetry import RelayTelemetry
from ..state_shadow import StateShadow

# A device that has not been heard from within this window is shown as stale
# rather than online, independently of the 72h identity retention.
LOCAL_PRESENCE_GRACE_SECONDS = 90.0


class DashboardContext:
    """Expose safe snapshots of existing relay components to HTTP handlers."""

    def __init__(
        self,
        config: RelayConfig,
        registry: DeviceRegistry,
        queue: MessageQueue,
        shadow: StateShadow,
        telemetry: RelayTelemetry,
        logs: RingBufferLogHandler,
        devices: DeviceManager,
    ) -> None:
        self._config = config
        self._registry = registry
        self._queue = queue
        self._shadow = shadow
        self._telemetry = telemetry
        self._logs = logs
        self._devices = devices
        self._lock = threading.Lock()
        self._device_addresses: dict[str, str] = {}
        self._locally_online: set[str] = set()
        self._last_session_ended_at: dict[str, float] = {}

    @property
    def logs(self) -> RingBufferLogHandler:
        """Return the sanitized ring buffer used by the SSE endpoint."""
        return self._logs

    # -- presence ----------------------------------------------------------------

    def set_device_online(self, device_id: str, peer_address: str) -> None:
        """Record that a device currently holds a local session."""
        with self._lock:
            self._device_addresses[device_id] = peer_address
            self._locally_online.add(device_id)

    def set_device_offline(self, device_id: str) -> None:
        """Record that a device's last local session has ended."""
        with self._lock:
            self._locally_online.discard(device_id)
            self._last_session_ended_at[device_id] = time.time()

    # -- aggregate views ---------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return the compact overview used by the dashboard header."""
        telemetry = self._telemetry.snapshot()
        devices = self.devices()
        summary = devices["summary"]
        return cast(
            dict[str, Any],
            sanitize_value(
                {
                    "relay": {
                        "status": "running",
                        "uptime_seconds": telemetry["uptime_seconds"],
                        "local_responder_enabled": self._config.local_responder.enabled,
                        "mode": (
                            "LOCAL_FALLBACK"
                            if self._config.local_responder.enabled
                            else "PURE_PIPE"
                        ),
                        "auto_enroll": self._config.auto_enroll,
                    },
                    "local_mqtt": telemetry["local_mqtt"],
                    "devices": summary,
                    "queues": self.queue_totals(),
                    "local_responder": self.responder_settings(),
                }
            ),
        )

    def devices(self) -> dict[str, Any]:
        """Return one row per known device plus the aggregate summary."""
        entries = {entry.client_id: entry for entry in self._registry.entries()}
        rows = [self._device_row(entry) for entry in entries.values()]
        rows.sort(key=lambda row: str(row["device_id"]))
        return cast(
            dict[str, Any],
            sanitize_value(
                {
                    "devices": rows,
                    "summary": _summarize(rows, self._config.auto_enroll),
                    "retention_seconds": self._registry.retention_seconds,
                }
            ),
        )

    def queue_totals(self) -> dict[str, Any]:
        """Return queue depth across every device, for the header and overview."""
        depth_by_device = self._queue.depth_by_device()
        return {
            "pending_total": sum(depth_by_device.values()),
            "pending_by_device": depth_by_device,
            "unroutable": self._queue.unroutable_count(),
        }

    def cloud(self) -> dict[str, Any]:
        """Return every device's upstream state and the shared event timeline."""
        return cast(
            dict[str, Any],
            sanitize_value(
                {
                    "devices": self._telemetry.device_snapshots(),
                    "events": self._telemetry.events(500),
                }
            ),
        )

    # -- per-device views ---------------------------------------------------------

    def device_detail(self, device_id: str, raw_limit: int = 100) -> dict[str, Any] | None:
        """Return everything the dashboard shows for one device, or None."""
        entry = next(
            (item for item in self._registry.entries() if item.client_id == device_id), None
        )
        if entry is None:
            return None
        return cast(
            dict[str, Any],
            sanitize_value(
                {
                    "device": self._device_row(entry),
                    "cloud": {
                        "metrics": self._device_metrics(device_id),
                        "events": self._telemetry.events(200, device_id=device_id),
                    },
                    "queues": self.queues(device_id, limit=100),
                    "state": self.state(device_id, raw_limit),
                    "ntp": self.ntp(device_id),
                    "local_responder": self.responder(device_id),
                }
            ),
        )

    def queues(self, device_id: str, limit: int) -> dict[str, Any]:
        """Return bounded metadata for one device's two durable directions."""
        return cast(
            dict[str, Any],
            sanitize_value(
                {
                    "device_id": device_id,
                    "device_to_cloud": self._queue.snapshot(device_id, LOCAL_TO_UPSTREAM, limit),
                    "cloud_to_device": self._queue.snapshot(device_id, UPSTREAM_TO_LOCAL, limit),
                }
            ),
        )

    def state(self, device_id: str, raw_limit: int) -> dict[str, Any]:
        """Return one device's shadow, scoped strictly to that device."""
        return cast(
            dict[str, Any],
            sanitize_value(self._shadow.dashboard_snapshot(device_id, raw_limit)),
        )

    def ntp(self, device_id: str) -> dict[str, Any]:
        """Return one device's NTP observations without inferring replies."""
        state = self.state(device_id, raw_limit=100)
        counters = self._device_metrics(device_id)["upstream"]["counters"]
        raw_messages = state["raw_messages"]
        responder = self.responder(device_id)
        return {
            "device_id": device_id,
            "trigger": "session_establishment",
            "requests_observed": counters.get("ntp_requests", 0),
            "cloud_ntp_sync_responses": counters.get("ntp_sync_from_cloud", 0),
            "local_ntp_responses": counters.get("local_responses", 0),
            "local_fallback_enabled": responder["ntp_enabled"],
            "last_request": next((item for item in raw_messages if item["cmd"] == "NTP"), None),
            "last_ntp_sync": next(
                (item for item in raw_messages if item["cmd"] == "NTP_SYNC"), None
            ),
        }

    def responder(self, device_id: str) -> dict[str, Any]:
        """Return one device's responder flags and counters."""
        context = self._devices.get_by_device_id(device_id)
        if context is not None and context.responder is not None:
            return context.responder.snapshot()
        return self.responder_settings()

    def responder_settings(self) -> dict[str, Any]:
        """Return configured responder flags, with no per-device counters."""
        settings = self._config.local_responder
        return {
            "enabled": settings.enabled,
            "ntp_enabled": settings.ntp,
            "config_enabled": settings.config,
            "feeding_plan_enabled": settings.feeding_plan,
            "always_answer_ntp_locally": settings.always_answer_ntp_locally,
            "device_timezone": settings.device_timezone,
            "counters": {},
        }

    def system(self) -> dict[str, Any]:
        """Return local process/database facts for diagnostics only."""
        return {
            "version": "0.1.0",
            "git_commit": os.environ.get("PETLIBRO_GIT_COMMIT", "unknown"),
            "hostname": platform.node(),
            "python_version": platform.python_version(),
            "pid": os.getpid(),
            "auto_enroll": self._config.auto_enroll,
            "databases": [
                _file_metadata("registry", self._config.device_registry_db_path),
                _file_metadata("queue", self._config.queue_db_path),
                _file_metadata("state_shadow", self._config.state_shadow_db_path),
            ],
            "runtime": _runtime_metadata(Path(self._config.state_shadow_db_path).parent),
        }

    # -- internals ----------------------------------------------------------------

    def _device_metrics(self, device_id: str) -> dict[str, Any]:
        """Return a device's telemetry, or an empty shape if it never started."""
        for snapshot in self._telemetry.device_snapshots():
            if snapshot["device_id"] == device_id:
                return snapshot
        return {
            "device_id": device_id,
            "local": {"online": False, "last_seen_at": None},
            "upstream": {
                "state": "DISCONNECTED",
                "last_connack_0": None,
                "last_disconnect": None,
                "current_online_duration_seconds": None,
                "previous_session_duration_seconds": None,
                "average_session_duration_seconds": None,
                "minimum_session_duration_seconds": None,
                "maximum_session_duration_seconds": None,
                "counters": {},
                "outage": {
                    "started_at": None,
                    "downtime_seconds": None,
                    "attempts": 0,
                    "failed_attempts": 0,
                    "last_reason": None,
                },
                "availability": {"15m": None, "1h": None, "24h": None},
            },
        }

    def _device_row(self, entry: DeviceRegistryEntry) -> dict[str, Any]:
        """Build one dashboard row, never exposing the device's password."""
        metrics = self._device_metrics(entry.client_id)
        reported = self._shadow.get_reported(entry.client_id)
        with self._lock:
            address = self._device_addresses.get(entry.client_id)
            locally_online = entry.client_id in self._locally_online
            session_ended_at = self._last_session_ended_at.get(entry.client_id)
        now = time.time()
        last_heartbeat = reported.get("last_heartbeat_ts")
        return {
            "device_id": entry.client_id,
            "client_id": entry.client_id,
            "product_id": entry.product_id,
            "username": mask_username(entry.username),
            "status": entry.status.value,
            "bridged": entry.bridged,
            "local_state": _local_state(locally_online, session_ended_at, now),
            "cloud_state": metrics["upstream"]["state"],
            "ip": address,
            "firmware": reported.get("firmware"),
            "hardware_version": reported.get("hardware_version"),
            "mac": reported.get("mac"),
            "rssi": reported.get("rssi"),
            "last_heartbeat_ts": last_heartbeat,
            "first_seen_at": entry.first_seen_at,
            "last_seen_at": entry.last_seen_at,
            "age_seconds": max(0.0, now - entry.last_seen_at),
            "ttl_remaining_seconds": max(
                0.0, self._registry.retention_seconds - (now - entry.last_seen_at)
            ),
            "queue_pending": (
                self._queue.count(entry.client_id, LOCAL_TO_UPSTREAM)
                + self._queue.count(entry.client_id, UPSTREAM_TO_LOCAL)
            ),
            "availability": metrics["upstream"]["availability"],
        }


def _local_state(locally_online: bool, session_ended_at: float | None, now: float) -> str:
    """Report presence from real sessions only, never from identity retention.

    A known identity is not a connected device: the registry keeps entries for
    72h, and even a freshly recorded one only means the relay learned it - a
    manually seeded device has never connected at all. Presence therefore
    comes exclusively from local sessions the capture proxy actually observed.

    A session that ended moments ago still counts as online, to ride out the
    gap between a feeder dropping its link and reconnecting; a device that has
    never held one is offline.
    """
    if locally_online:
        return "LOCAL_ONLINE"
    if session_ended_at is not None and now - session_ended_at <= LOCAL_PRESENCE_GRACE_SECONDS:
        return "LOCAL_ONLINE"
    return "LOCAL_OFFLINE"


def _summarize(rows: list[dict[str, Any]], auto_enroll: bool) -> dict[str, Any]:
    """Reduce the device rows to the counts the header and overview show."""
    bridged = [row for row in rows if row["bridged"]]
    return {
        "known": len(rows),
        "bridged": len(bridged),
        "awaiting_enrollment": sum(1 for row in rows if not row["bridged"]),
        "local_online": sum(1 for row in rows if row["local_state"] == "LOCAL_ONLINE"),
        "cloud_online": sum(1 for row in bridged if row["cloud_state"] == "ONLINE"),
        "cloud_degraded": sum(1 for row in bridged if row["cloud_state"] != "ONLINE"),
        "queue_pending": sum(int(row["queue_pending"]) for row in rows),
        "auto_enroll": auto_enroll,
    }


def _file_metadata(name: str, path: str) -> dict[str, Any]:
    """Return safe database metadata without reading its contents."""
    candidate = Path(path)
    return {
        "name": name,
        "path": str(candidate),
        "size_bytes": candidate.stat().st_size if candidate.exists() else 0,
    }


def _runtime_metadata(storage_path: Path) -> dict[str, Any]:
    """Collect light host metrics without adding a monitoring dependency."""
    disk = shutil.disk_usage(storage_path)
    return {
        "cpu_load_1m": os.getloadavg()[0] if hasattr(os, "getloadavg") else None,
        "memory": _memory_metadata(),
        "disk": {"total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free},
    }


def _memory_metadata() -> dict[str, int] | None:
    """Read Linux container memory facts when procfs is available."""
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    values = {line.split(":", 1)[0]: int(line.split()[1]) * 1024 for line in lines if ":" in line}
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None:
        return None
    return {"total_bytes": total, "used_bytes": total - available, "available_bytes": available}
