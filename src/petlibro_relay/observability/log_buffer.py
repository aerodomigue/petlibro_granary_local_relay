"""In-memory, sanitized ring buffer backing dashboard logs and SSE."""

from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from .sanitizer import sanitize_text

DEFAULT_LOG_BUFFER_SIZE = 5000


@dataclass(frozen=True, slots=True)
class BufferedLogEntry:
    """A serializable, sanitized log record for the web dashboard."""

    sequence: int
    timestamp: float
    level: str
    component: str
    message: str

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
        "web": "web",
    }
    for suffix, component in mapping.items():
        if logger_name.endswith(suffix):
            return component
    return "system"
