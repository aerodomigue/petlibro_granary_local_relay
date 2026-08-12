"""Read-only dependency container for the relay dashboard."""

from __future__ import annotations

import os
import platform
import shutil
import threading
import time
from pathlib import Path
from typing import Any, cast

from ..config import RelayConfig
from ..device_registry import DeviceIdentity, DeviceRegistry, DeviceRegistryEntry
from ..local_responder import LocalResponder
from ..message_queue import MessageQueue
from ..observability.log_buffer import RingBufferLogHandler
from ..observability.sanitizer import mask_username, sanitize_value
from ..observability.telemetry import RelayTelemetry
from ..state_shadow import StateShadow


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
    ) -> None:
        self._config = config
        self._registry = registry
        self._queue = queue
        self._shadow = shadow
        self._telemetry = telemetry
        self._logs = logs
        self._lock = threading.Lock()
        self._identity: DeviceIdentity | None = None
        self._responder: LocalResponder | None = None
        self._device_ip: str | None = None

    @property
    def logs(self) -> RingBufferLogHandler:
        """Return the sanitized ring buffer used by the SSE endpoint."""
        return self._logs

    def set_active_device(self, identity: DeviceIdentity, responder: LocalResponder) -> None:
        """Attach the mono-device bridge once the CONNECT learner resolves it."""
        with self._lock:
            self._identity = identity
            self._responder = responder

    def set_device_ip(self, device_ip: str) -> None:
        """Record the latest source address observed by the TCP proxy."""
        with self._lock:
            self._device_ip = device_ip

    def status(self) -> dict[str, Any]:
        """Return the compact overview used by the dashboard header."""
        telemetry = self._telemetry.snapshot()
        devices = self.devices()
        state = self.state(raw_limit=1)
        queues = self.queues(limit=1)
        return cast(dict[str, Any], sanitize_value(
            {
                "relay": {
                    "status": "running",
                    "uptime_seconds": telemetry["uptime_seconds"],
                    "local_responder_enabled": self._config.local_responder.enabled,
                    "mode": "LOCAL_FALLBACK" if self._config.local_responder.enabled else "PURE_PIPE",
                },
                "local_mqtt": telemetry["local_mqtt"],
                "upstream": telemetry["upstream"],
                "device": devices["active"],
                "queues": {
                    "device_to_cloud": queues["device_to_cloud"]["pending"],
                    "cloud_to_device": queues["cloud_to_device"]["pending"],
                },
                "state_shadow": state["counts"],
                "local_responder": self.responder(),
            }
        ))

    def cloud(self) -> dict[str, Any]:
        """Return upstream MQTT state and its bounded internal timeline."""
        metrics = self._telemetry.snapshot()["upstream"]
        return cast(dict[str, Any], sanitize_value({"upstream": metrics, "events": self._telemetry.events(500)}))

    def devices(self) -> dict[str, Any]:
        """Return active/candidate registry metadata with username masking."""
        snapshot = self._registry.snapshot()
        active = _registry_entry(cast(DeviceRegistryEntry | None, snapshot["active"]))
        candidates = [
            _registry_entry(entry)
            for entry in cast(list[DeviceRegistryEntry], snapshot["candidates"])
        ]
        with self._lock:
            identity = self._identity
            device_ip = self._device_ip
        if active is not None and identity is not None:
            active["model"] = "PLAF203"
            reported = self._shadow.get_reported(identity.client_id)
            for key in ("firmware", "rssi", "last_heartbeat_ts", "mac"):
                if key in reported:
                    active[key] = reported[key]
            active["ttl_remaining_seconds"] = max(
                0.0, cast(float, snapshot["retention_seconds"]) - float(active["age_seconds"])
            )
            active["ip"] = device_ip
        return {
            "active": active,
            "candidates": candidates,
            "retention_seconds": cast(float, snapshot["retention_seconds"]),
        }

    def queues(self, limit: int) -> dict[str, Any]:
        """Return bounded metadata for both durable message directions."""
        return cast(dict[str, Any], sanitize_value(
            {
                "device_to_cloud": self._queue.snapshot("local-to-upstream", limit),
                "cloud_to_device": self._queue.snapshot("upstream-to-local", limit),
            }
        ))

    def state(self, raw_limit: int) -> dict[str, Any]:
        """Return the active device's shadow or an empty safe shape."""
        with self._lock:
            identity = self._identity
        if identity is None:
            return _empty_state_snapshot()
        return cast(dict[str, Any], sanitize_value(self._shadow.dashboard_snapshot(identity.client_id, raw_limit)))

    def ntp(self) -> dict[str, Any]:
        """Return NTP session-establishment observations without inferring replies."""
        state = self.state(raw_limit=100)
        counters = self._telemetry.snapshot()["upstream"]["counters"]
        raw_messages = state["raw_messages"]
        last_request = next((item for item in raw_messages if item["cmd"] == "NTP"), None)
        last_sync = next((item for item in raw_messages if item["cmd"] == "NTP_SYNC"), None)
        responder = self.responder()
        return {
            "trigger": "session_establishment",
            "requests_observed": counters.get("ntp_requests", 0),
            "cloud_ntp_sync_responses": counters.get("ntp_sync_from_cloud", 0),
            "local_ntp_responses": responder["counters"].get("local_responses", 0),
            "local_fallback_enabled": responder["ntp_enabled"],
            "last_request": last_request,
            "last_ntp_sync": last_sync,
        }

    def responder(self) -> dict[str, Any]:
        """Return local-responder flags and counters even before a device appears."""
        with self._lock:
            responder = self._responder
        if responder is not None:
            return responder.snapshot()
        return {
            "enabled": self._config.local_responder.enabled,
            "ntp_enabled": self._config.local_responder.ntp,
            "config_enabled": self._config.local_responder.config,
            "feeding_plan_enabled": self._config.local_responder.feeding_plan,
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
            "databases": [
                _file_metadata("registry", self._config.device_registry_db_path),
                _file_metadata("queue", self._config.queue_db_path),
                _file_metadata("state_shadow", self._config.state_shadow_db_path),
            ],
            "runtime": _runtime_metadata(Path(self._config.state_shadow_db_path).parent),
        }


def _registry_entry(entry: DeviceRegistryEntry | None) -> dict[str, Any] | None:
    """Serialize an entry while ensuring usernames are never shown fully."""
    if entry is None:
        return None
    return {
        "client_id": entry.client_id,
        "username": mask_username(entry.username),
        "first_seen_at": entry.first_seen_at,
        "last_seen_at": entry.last_seen_at,
        "status": "ACTIVE" if entry.active else "CANDIDATE",
        "age_seconds": max(0.0, time.time() - entry.last_seen_at),
    }


def _empty_state_snapshot() -> dict[str, Any]:
    """Use a stable schema while the first device identity is still unknown."""
    return {
        "device_id": None,
        "reported": [],
        "desired": [],
        "feeding_plans": {"plans": [], "source_msg_id": None, "updated_at": None, "complete": False},
        "raw_messages": [],
        "counts": {"reported": 0, "desired": 0, "raw_messages": 0, "feeding_plan_cached": False},
    }


def _file_metadata(name: str, path: str) -> dict[str, Any]:
    """Return safe database metadata without reading its contents."""
    candidate = Path(path)
    return {"name": name, "path": str(candidate), "size_bytes": candidate.stat().st_size if candidate.exists() else 0}


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
