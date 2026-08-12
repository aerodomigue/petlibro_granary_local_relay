"""Thread-safe runtime counters and event history for read-only diagnostics."""

from __future__ import annotations

import collections
import threading
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from ..local_responder import UpstreamState

EVENT_HISTORY_LIMIT = 500


class UpstreamFailureKind(StrEnum):
    """Classifications that distinguish MQTT failures from TCP availability."""

    CONNECT_TIMEOUT = "connect_timeout"
    TCP_FAILURE = "tcp_failure"
    SOCKET_RESET = "socket_reset"
    CLEAN_DISCONNECT = "clean_disconnect"
    OTHER_DISCONNECT = "other_disconnect"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """Small retained event for Overview and Cloud timelines."""

    timestamp: float
    kind: str
    message: str
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-compatible event data."""
        return asdict(self)


class RelayTelemetry:
    """Owns runtime-only metrics; SQLite remains source of durable relay state."""

    def __init__(self, started_at: float | None = None) -> None:
        self._lock = threading.Lock()
        self._started_at = started_at if started_at is not None else time.time()
        self._local_connected = False
        self._upstream_state = UpstreamState.DISCONNECTED
        self._last_connack_at: float | None = None
        self._last_disconnect_at: float | None = None
        self._last_online_started_at: float | None = None
        self._state_history: collections.deque[tuple[float, bool]] = collections.deque(maxlen=EVENT_HISTORY_LIMIT)
        self._session_durations: collections.deque[float] = collections.deque(maxlen=500)
        self._counters: collections.Counter[str] = collections.Counter()
        self._events: collections.deque[RuntimeEvent] = collections.deque(maxlen=EVENT_HISTORY_LIMIT)

    def record_event(self, kind: str, message: str, **details: Any) -> None:
        """Append a bounded dashboard event."""
        with self._lock:
            self._events.append(RuntimeEvent(time.time(), kind, message, details))

    def local_connected(self) -> None:
        """Mark local MQTT usable."""
        with self._lock:
            self._local_connected = True
        self.record_event("local_connected", "Local MQTT connected")

    def local_disconnected(self) -> None:
        """Mark local MQTT unavailable."""
        with self._lock:
            self._local_connected = False
        self.record_event("local_disconnected", "Local MQTT disconnected")

    def upstream_connect_attempt(self) -> None:
        """Record start of a MQTT CONNECT attempt (not yet online)."""
        with self._lock:
            self._counters["connect_attempts"] += 1
            self._upstream_state = UpstreamState.MQTT_CONNECTING
        self.record_event("upstream_connect", "Upstream MQTT CONNECT sent")

    def upstream_online(self) -> None:
        """Mark MQTT online only after CONNACK 0."""
        now = time.time()
        with self._lock:
            self._upstream_state = UpstreamState.ONLINE
            self._last_connack_at = now
            self._last_online_started_at = now
            self._counters["connack_success"] += 1
            self._state_history.append((now, True))
        self.record_event("upstream_online", "PETLIBRO MQTT CONNACK 0 received")

    def upstream_refused(self) -> None:
        """Record a broker-level CONNACK rejection."""
        with self._lock:
            self._counters["connack_refused"] += 1
            self._upstream_state = UpstreamState.DISCONNECTED
        self.record_event("upstream_refused", "PETLIBRO MQTT CONNACK refused")

    def upstream_connect_failed(self) -> None:
        """Record failure before the TCP connection exists."""
        with self._lock:
            self._counters["tcp_failures"] += 1
            self._upstream_state = UpstreamState.DISCONNECTED
        self.record_event("upstream_tcp_failure", "Upstream TCP/DNS connection failed")

    def upstream_disconnected(self, reason: str) -> None:
        """Record disconnected session and classify the visible reason."""
        now = time.time()
        with self._lock:
            if self._upstream_state is UpstreamState.MQTT_CONNECTING:
                self._counters["connack_timeouts"] += 1
                kind = UpstreamFailureKind.CONNECT_TIMEOUT
            elif reason == "Normal disconnection":
                self._counters["clean_disconnects"] += 1
                kind = UpstreamFailureKind.CLEAN_DISCONNECT
            else:
                self._counters["disconnects"] += 1
                kind = UpstreamFailureKind.OTHER_DISCONNECT
            if self._last_online_started_at is not None:
                self._session_durations.append(now - self._last_online_started_at)
            self._last_online_started_at = None
            self._last_disconnect_at = now
            self._upstream_state = UpstreamState.DISCONNECTED
            self._state_history.append((now, False))
        self.record_event("upstream_disconnected", "PETLIBRO MQTT disconnected", reason=reason, classification=kind)

    def increment(self, counter: str, amount: int = 1) -> None:
        """Increase a named operation counter."""
        with self._lock:
            self._counters[counter] += amount

    def snapshot(self) -> dict[str, Any]:
        """Return one consistent, JSON-ready view of volatile relay metrics."""
        now = time.time()
        with self._lock:
            current_duration = (
                now - self._last_online_started_at if self._last_online_started_at is not None else None
            )
            durations = list(self._session_durations)
            return {
                "started_at": self._started_at,
                "uptime_seconds": now - self._started_at,
                "local_mqtt": {"connected": self._local_connected},
                "upstream": {
                    "state": self._upstream_state.name,
                    "last_connack_0": self._last_connack_at,
                    "last_disconnect": self._last_disconnect_at,
                    "current_online_duration_seconds": current_duration,
                    "previous_session_duration_seconds": durations[-1] if durations else None,
                    "average_session_duration_seconds": sum(durations) / len(durations) if durations else None,
                    "minimum_session_duration_seconds": min(durations) if durations else None,
                    "maximum_session_duration_seconds": max(durations) if durations else None,
                    "counters": dict(self._counters),
                    "availability": {
                        "15m": self._availability_locked(now, 15 * 60),
                        "1h": self._availability_locked(now, 60 * 60),
                        "24h": self._availability_locked(now, 24 * 60 * 60),
                    },
                },
            }

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return newest retained events without exposing mutable structures."""
        safe_limit = max(1, min(limit, EVENT_HISTORY_LIMIT))
        with self._lock:
            return [event.as_dict() for event in list(self._events)[-safe_limit:]]

    def _availability_locked(self, now: float, window_seconds: float) -> float | None:
        """Estimate ONLINE share from retained state transitions.

        A window that predates this process cannot be measured honestly, so
        only the observed part since process start is used. `None` means no
        transition/online state has been observed yet.
        """
        window_start = max(self._started_at, now - window_seconds)
        transitions = [(timestamp, online) for timestamp, online in self._state_history if timestamp >= window_start]
        if not transitions and self._last_online_started_at is None:
            return None
        state = False
        for timestamp, online in self._state_history:
            if timestamp <= window_start:
                state = online
            else:
                break
        cursor = window_start
        online_seconds = 0.0
        for timestamp, online in transitions:
            if state:
                online_seconds += timestamp - cursor
            cursor = timestamp
            state = online
        if state:
            online_seconds += now - cursor
        observed_seconds = now - window_start
        return online_seconds / observed_seconds if observed_seconds > 0 else None
