"""State projections and narrow control dependencies for the relay dashboard.

Dashboard methods project state only. Explicit device-control paths are kept
outside this class in ``SoundSwitchController`` and injected as an allowlist.
"""

from __future__ import annotations

import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any, cast

from ..camera import CameraStatusProvider, Go2RtcCameraClient, Go2RtcStreamClient, WebRtcExchange
from ..config import RelayConfig
from ..device_context import LOCAL_TO_UPSTREAM, UPSTREAM_TO_LOCAL
from ..device_manager import DeviceManager
from ..device_presence import DevicePresenceTracker, LocalPresence
from ..device_registry import DeviceRegistry, DeviceRegistryEntry
from ..message_queue import MessageQueue
from ..observability.log_buffer import RingBufferLogHandler
from ..observability.sanitizer import mask_username, sanitize_value
from ..observability.telemetry import RelayTelemetry
from ..state_shadow import StateShadow
from ..sound_switch_control import SoundSwitchController


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
        presence: DevicePresenceTracker,
        sound_switch_control: SoundSwitchController | None = None,
        camera: CameraStatusProvider | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._queue = queue
        self._shadow = shadow
        self._telemetry = telemetry
        self._logs = logs
        self._devices = devices
        self._presence = presence
        self._sound_switch_control = sound_switch_control
        self._camera = camera or Go2RtcCameraClient(config.go2rtc)
        self._camera_streams = Go2RtcStreamClient(config.go2rtc)

    @property
    def logs(self) -> RingBufferLogHandler:
        """Return the sanitized ring buffer used by the SSE endpoint."""
        return self._logs

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
                    "controls": self.controls(device_id),
                    "camera": self.camera(device_id, entry.product_id),
                }
            ),
        )

    def camera(self, device_id: str, product_id: str | None = None) -> dict[str, object]:
        """Return the safe go2rtc camera status for one known device."""
        if product_id is None:
            entry = next(
                (item for item in self._registry.entries() if item.client_id == device_id), None
            )
            if entry is None:
                return {}
            product_id = entry.product_id
        status = self._camera.status(device_id, product_id)
        return {
            **status.snapshot(),
            "uid_learned": self._shadow.get_camera_uid(device_id) is not None,
        }

    def activate_camera_viewer(self, device_id: str, viewer_id: str) -> bool:
        """Register a logical viewer before WHEP negotiation."""
        return self._camera_streams.activate_viewer(device_id, viewer_id)

    def heartbeat_camera_viewer(self, device_id: str, viewer_id: str) -> bool:
        """Refresh an existing logical viewer."""
        return self._camera_streams.heartbeat_viewer(device_id, viewer_id)

    def deactivate_camera_viewer(self, device_id: str, viewer_id: str, reason: str) -> bool:
        """Remove one logical viewer."""
        return self._camera_streams.deactivate_viewer(device_id, viewer_id, reason)

    def exchange_camera_webrtc(self, device_id: str, viewer_id: str, offer: bytes) -> WebRtcExchange:
        """Proxy one validated device SDP offer without accepting arbitrary streams."""
        return self._camera_streams.exchange_webrtc(device_id, viewer_id, offer)

    def close_camera_webrtc(self, device_id: str, session_id: str) -> bool:
        """Release one opaque browser WHEP session without exposing go2rtc."""
        return self._camera_streams.close_webrtc(device_id, session_id)

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

    def controls(self, device_id: str) -> dict[str, Any]:
        """Return explicit capability state without exposing a generic control path."""
        if self._sound_switch_control is None:
            return {
                "soundSwitch": {
                    "control": "soundSwitch",
                    "writable": False,
                    "device_ack_confirmed": True,
                    "cloud_sync_confirmed": True,
                    "device_online": False,
                    "required_state_available": False,
                    "pending": False,
                },
                "motionDetectionSwitch": {
                    "control": "motionDetectionSwitch",
                    "writable": False,
                    "device_ack_confirmed": True,
                    "cloud_sync_confirmed": False,
                    "device_online": False,
                    "required_state_available": False,
                    "pending": False,
                },
                "counters": {},
            }
        return self._sound_switch_control.snapshot(device_id)

    @property
    def sound_switch_control(self) -> SoundSwitchController | None:
        """Return the narrow service used by explicit write endpoints."""
        return self._sound_switch_control

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
        presence = self._presence.record(entry.client_id)
        context = self._devices.get_by_device_id(entry.client_id)
        now = time.time()
        last_heartbeat = reported.get("last_heartbeat_ts")
        return {
            "device_id": entry.client_id,
            "client_id": entry.client_id,
            "product_id": entry.product_id,
            "username": mask_username(entry.username),
            "status": entry.status.value,
            "bridged": entry.bridged,
            "local_state": self._presence.state(entry.client_id).value,
            "cloud_state": metrics["upstream"]["state"],
            "upstream_running": context.upstream_running if context is not None else False,
            "last_local_session_at": presence.last_opened_at if presence is not None else None,
            "ip": presence.peer_address if presence is not None else None,
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


def _summarize(rows: list[dict[str, Any]], auto_enroll: bool) -> dict[str, Any]:
    """Reduce the device rows to the counts the header and overview show."""
    bridged = [row for row in rows if row["bridged"]]
    connected = [row for row in bridged if row["local_state"] == LocalPresence.ONLINE.value]
    return {
        "known": len(rows),
        "bridged": len(bridged),
        "awaiting_enrollment": sum(1 for row in rows if not row["bridged"]),
        "local_online": sum(1 for row in rows if row["local_state"] == LocalPresence.ONLINE.value),
        "cloud_online": sum(1 for row in bridged if row["cloud_state"] == "ONLINE"),
        # Only devices that are actually here can be "degraded": a suspended
        # session for an absent feeder is the intended state, not a fault.
        "cloud_degraded": sum(1 for row in connected if row["cloud_state"] != "ONLINE"),
        "cloud_suspended": sum(1 for row in bridged if row["cloud_state"] == "SUSPENDED"),
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
