"""In-memory, sanitized ring buffer backing dashboard logs and SSE."""

from __future__ import annotations

import collections
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from .sanitizer import sanitize_text

DEFAULT_LOG_BUFFER_SIZE = 5000
DEVICE_TOPIC_PATTERN = re.compile(r"dl/[^/]+/([^/]+)/")
CLIENT_ID_PATTERN = re.compile(r"client_id=([A-Za-z0-9_-]+)")
DEVICE_ID_FIELD_PATTERN = re.compile(r"device_id=([A-Za-z0-9._-]+)")
COMMAND_PATTERN = re.compile(r"\bcmd[= ]([A-Z][A-Z0-9_]+)")


@dataclass(frozen=True, slots=True)
class BufferedLogEntry:
    """A serializable, sanitized log record for the web dashboard."""

    sequence: int
    timestamp: float
    level: str
    component: str
    message: str
    device_id: str | None
    cmd: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-compatible representation."""
        return asdict(self)


class RingBufferLogHandler(logging.Handler):
    """Logging handler retaining recent safe records without blocking emitters."""

    def __init__(self, max_entries: int = DEFAULT_LOG_BUFFER_SIZE) -> None:
        super().__init__()
        self._entries: collections.deque[BufferedLogEntry] = collections.deque(maxlen=max_entries)
        self._condition = threading.Condition()
        self._next_sequence = 1

    def emit(self, record: logging.LogRecord) -> None:
        """Append one record, swallowing handler failures to protect the relay."""
        try:
            rendered = self.format(record)
            with self._condition:
                entry = BufferedLogEntry(
                    sequence=self._next_sequence,
                    timestamp=record.created,
                    level=record.levelname,
                    component=_component_from_logger(record.name),
                    message=sanitize_text(rendered),
                    device_id=_device_id_from_record(record, rendered),
                    cmd=_command_from_record(record, rendered),
                )
                self._next_sequence += 1
                self._entries.append(entry)
                self._condition.notify_all()
        except (TypeError, ValueError):
            self.handleError(record)

    def snapshot(self, limit: int = 500) -> list[dict[str, Any]]:
        """Return the most recent entries, bounded to protect the API."""
        safe_limit = max(1, min(limit, DEFAULT_LOG_BUFFER_SIZE))
        with self._condition:
            return [entry.as_dict() for entry in list(self._entries)[-safe_limit:]]

    def wait_after(self, sequence: int, timeout_seconds: float) -> list[dict[str, Any]]:
        """Wait for records newer than `sequence` for SSE streaming."""
        with self._condition:
            has_new = bool(self._entries and self._entries[-1].sequence > sequence)
            if not has_new:
                self._condition.wait(timeout_seconds)
            return [entry.as_dict() for entry in self._entries if entry.sequence > sequence]


def _component_from_logger(logger_name: str | None) -> str:
    """Map module names into small stable dashboard component labels."""
    if logger_name is None:
        return "system"
    mapping = {
        "credential_capture_proxy": "proxy",
        "device_registry": "registry",
        "mqtt_bridge": "bridge",
        "delivery_pump": "replay",
        "message_queue": "queue",
        "state_shadow": "shadow",
        "local_responder": "responder",
        "sound_switch_control": "control",
        "web": "web",
    }
    for suffix, component in mapping.items():
        if logger_name.endswith(suffix):
            return component
    return "system"


def _device_id_from_record(record: logging.LogRecord, message: str) -> str | None:
    """Extract a non-secret device identifier when a log line contains one."""
    explicit = getattr(record, "device_id", None)
    if isinstance(explicit, str):
        return explicit
    topic_match = DEVICE_TOPIC_PATTERN.search(message)
    if topic_match is not None:
        return topic_match.group(1)
    device_match = DEVICE_ID_FIELD_PATTERN.search(message)
    if device_match is not None:
        return device_match.group(1)
    client_match = CLIENT_ID_PATTERN.search(message)
    return client_match.group(1) if client_match is not None else None


def _command_from_record(record: logging.LogRecord, message: str) -> str | None:
    """Extract an optional MQTT command for client-side log filtering."""
    explicit = getattr(record, "cmd", None)
    if isinstance(explicit, str):
        return explicit
    match = COMMAND_PATTERN.search(message)
    return match.group(1) if match is not None else None
