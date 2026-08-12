"""Thread-safe transition-aware runtime telemetry for relay observability."""

from __future__ import annotations

import collections
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from ..local_responder import UpstreamState

EVENT_HISTORY_LIMIT = 500
OFFLINE_SUMMARY_INTERVAL_SECONDS = 5 * 60


class UpstreamTransitionKind(StrEnum):
    """Meaningful upstream state changes exposed to logs and the dashboard."""

    CONNECT_ATTEMPT = "connect_attempt"
    ONLINE = "online"
    SESSION_LOST = "session_lost"
    RETRY_FAILED = "retry_failed"
    TCP_CONNECT_FAILED = "tcp_connect_failed"
    CONNACK_REFUSED = "connack_refused"
    RESTORED = "restored"


@dataclass(frozen=True, slots=True)
class UpstreamTransition:
    """One classified upstream transition with safe structured context."""

    kind: UpstreamTransitionKind
    state_before: str
    state_after: str
    attempt: int
    reason: str | None = None
    reason_code: str | None = None
    disconnect_flags: str | None = None
    session_duration_seconds: float | None = None
    downtime_seconds: float | None = None
    failed_attempts: int = 0
    offline_summary_due: bool = False

    def details(self) -> dict[str, Any]:
        """Return a JSON-ready details object for the event timeline."""
        return asdict(self)


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
    """Own runtime metrics without changing MQTT connection behavior.

    The class owns the semantic state machine because Paho may invoke
    ``on_disconnect`` for a half-open connect attempt as well as for a real
    online session. A callback is therefore not intrinsically a session loss.
    """

    def __init__(
        self, started_at: float | None = None, clock: Callable[[], float] | None = None
    ) -> None:
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._started_at = started_at if started_at is not None else self._clock()
        self._local_connected = False
        self._upstream_state = UpstreamState.DISCONNECTED
        self._last_connack_at: float | None = None
        self._last_disconnect_at: float | None = None
        self._last_online_started_at: float | None = None
        self._outage_started_at: float | None = None
        self._outage_attempts = 0
        self._outage_failed_attempts = 0
        self._outage_had_session_loss = False
        self._last_offline_summary_at: float | None = None
        self._last_failure_reason: str | None = None
        self._session_number = 0
        self._state_history: collections.deque[tuple[float, bool]] = collections.deque(maxlen=EVENT_HISTORY_LIMIT)
        self._session_durations: collections.deque[float] = collections.deque(maxlen=500)
        self._counters: collections.Counter[str] = collections.Counter()
        self._events: collections.deque[RuntimeEvent] = collections.deque(maxlen=EVENT_HISTORY_LIMIT)

    def record_event(self, kind: str, message: str, **details: Any) -> None:
        """Append a bounded dashboard event unrelated to upstream state."""
        with self._lock:
            self._record_event_locked(self._clock(), kind, message, details)

    def local_connected(self) -> None:
        """Mark local MQTT usable."""
        with self._lock:
            self._local_connected = True
            self._record_event_locked(self._clock(), "local_connected", "Local MQTT connected", {})

    def local_disconnected(self) -> None:
        """Mark local MQTT unavailable."""
        with self._lock:
            self._local_connected = False
            self._record_event_locked(self._clock(), "local_disconnected", "Local MQTT disconnected", {})

    def upstream_connect_attempt(self) -> UpstreamTransition:
        """Record an expected MQTT CONNECT attempt, not an ONLINE session."""
        with self._lock:
            now = self._clock()
            before = self._upstream_state
            self._begin_outage_locked(now)
            self._outage_attempts += 1
            self._counters["connect_attempts"] += 1
            self._upstream_state = UpstreamState.MQTT_CONNECTING
            transition = UpstreamTransition(
                kind=UpstreamTransitionKind.CONNECT_ATTEMPT,
                state_before=before.name,
                state_after=self._upstream_state.name,
                attempt=self._outage_attempts,
            )
            self._record_transition_locked(now, transition, "Upstream MQTT CONNECT attempt")
            return transition

    def upstream_online(self) -> UpstreamTransition:
        """Mark online only after a successful CONNACK and close any outage."""
        with self._lock:
            now = self._clock()
            before = self._upstream_state
            self._begin_outage_locked(now)
            downtime = now - self._outage_started_at if self._outage_started_at is not None else None
            failed_attempts = self._outage_failed_attempts
            attempt = self._outage_attempts
            self._upstream_state = UpstreamState.ONLINE
            self._last_connack_at = now
            self._last_online_started_at = now
            self._session_number += 1
            self._counters["connack_success"] += 1
            self._state_history.append((now, True))
            transition = UpstreamTransition(
                kind=(
                    UpstreamTransitionKind.RESTORED
                    if self._outage_had_session_loss or failed_attempts > 0
                    else UpstreamTransitionKind.ONLINE
                ),
                state_before=before.name,
                state_after=self._upstream_state.name,
                attempt=attempt,
                downtime_seconds=downtime,
                failed_attempts=failed_attempts,
            )
            self._clear_outage_locked()
            self._record_transition_locked(
                now,
                transition,
                "UPSTREAM restored" if transition.kind is UpstreamTransitionKind.RESTORED else "UPSTREAM online",
            )
            return transition

    def upstream_refused(self, reason_code: str) -> UpstreamTransition:
        """Record a broker-level CONNACK rejection as a failed retry."""
        with self._lock:
            now = self._clock()
            before = self._upstream_state
            self._ensure_attempt_for_failure_locked(now)
            self._counters["connack_refused"] += 1
            self._register_retry_failure_locked(now, reason_code)
            self._upstream_state = UpstreamState.DISCONNECTED
            transition = UpstreamTransition(
                kind=UpstreamTransitionKind.CONNACK_REFUSED,
                state_before=before.name,
                state_after=self._upstream_state.name,
                attempt=self._outage_attempts,
                reason_code=reason_code,
                downtime_seconds=self._downtime_locked(now),
                failed_attempts=self._outage_failed_attempts,
                offline_summary_due=self._offline_summary_due_locked(now),
            )
            self._record_transition_locked(now, transition, "Upstream CONNACK refused")
            return transition

    def upstream_connect_failed(self, reason: str = "TCP/DNS connect failure") -> UpstreamTransition:
        """Record a TCP/DNS connection failure when Paho calls on_connect_fail."""
        with self._lock:
            now = self._clock()
            before = self._upstream_state
            self._ensure_attempt_for_failure_locked(now)
            self._counters["tcp_connect_failures"] += 1
            self._register_retry_failure_locked(now, reason)
            self._upstream_state = UpstreamState.DISCONNECTED
            transition = UpstreamTransition(
                kind=UpstreamTransitionKind.TCP_CONNECT_FAILED,
                state_before=before.name,
                state_after=self._upstream_state.name,
                attempt=self._outage_attempts,
                reason=reason,
                downtime_seconds=self._downtime_locked(now),
                failed_attempts=self._outage_failed_attempts,
                offline_summary_due=self._offline_summary_due_locked(now),
            )
            self._record_transition_locked(now, transition, "Upstream TCP connect failed")
            return transition

    def upstream_disconnected(
        self, reason: str, disconnect_flags: str | None = None
    ) -> UpstreamTransition:
        """Classify Paho on_disconnect as session loss or reconnect failure."""
        with self._lock:
            now = self._clock()
            before = self._upstream_state
            if before is UpstreamState.ONLINE:
                session_duration = (
                    now - self._last_online_started_at if self._last_online_started_at is not None else None
                )
                if session_duration is not None:
                    self._session_durations.append(session_duration)
                self._counters["sessions_lost"] += 1
                self._last_online_started_at = None
                self._last_disconnect_at = now
                self._upstream_state = UpstreamState.DISCONNECTED
                self._begin_outage_locked(now)
                self._outage_had_session_loss = True
                self._state_history.append((now, False))
                transition = UpstreamTransition(
                    kind=UpstreamTransitionKind.SESSION_LOST,
                    state_before=before.name,
                    state_after=self._upstream_state.name,
                    attempt=0,
                    reason=reason,
                    disconnect_flags=disconnect_flags,
                    session_duration_seconds=session_duration,
                )
                self._record_transition_locked(now, transition, "UPSTREAM lost")
                return transition

            self._ensure_attempt_for_failure_locked(now)
            if before is UpstreamState.MQTT_CONNECTING:
                self._counters["connack_timeouts"] += 1
            self._register_retry_failure_locked(now, reason)
            self._last_disconnect_at = now
            self._upstream_state = UpstreamState.DISCONNECTED
            transition = UpstreamTransition(
                kind=UpstreamTransitionKind.RETRY_FAILED,
                state_before=before.name,
                state_after=self._upstream_state.name,
                attempt=self._outage_attempts,
                reason=reason,
                disconnect_flags=disconnect_flags,
                downtime_seconds=self._downtime_locked(now),
                failed_attempts=self._outage_failed_attempts,
                offline_summary_due=self._offline_summary_due_locked(now),
            )
            self._record_transition_locked(now, transition, "Upstream reconnect failed")
            return transition

    def increment(self, counter: str, amount: int = 1) -> None:
        """Increase a named operation counter."""
        with self._lock:
            self._counters[counter] += amount

    def snapshot(self) -> dict[str, Any]:
        """Return one consistent, JSON-ready view of volatile relay metrics."""
        now = self._clock()
        with self._lock:
            current_duration = (
                now - self._last_online_started_at if self._last_online_started_at is not None else None
            )
            durations = list(self._session_durations)
            outage = {
                "started_at": self._outage_started_at,
                "downtime_seconds": now - self._outage_started_at if self._outage_started_at is not None else None,
                "attempts": self._outage_attempts,
                "failed_attempts": self._outage_failed_attempts,
                "last_reason": self._last_failure_reason,
            }
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
                    "outage": outage,
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

    def _begin_outage_locked(self, now: float) -> None:
        if self._outage_started_at is not None:
            return
        self._outage_started_at = now
        self._outage_attempts = 0
        self._outage_failed_attempts = 0
        self._outage_had_session_loss = False
        self._last_offline_summary_at = None
        self._last_failure_reason = None

    def _clear_outage_locked(self) -> None:
        self._outage_started_at = None
        self._outage_attempts = 0
        self._outage_failed_attempts = 0
        self._outage_had_session_loss = False
        self._last_offline_summary_at = None
        self._last_failure_reason = None

    def _ensure_attempt_for_failure_locked(self, now: float) -> None:
        self._begin_outage_locked(now)
        if self._upstream_state is UpstreamState.MQTT_CONNECTING:
            return
        self._outage_attempts += 1
        self._counters["connect_attempts"] += 1

    def _register_retry_failure_locked(self, now: float, reason: str) -> None:
        self._counters["reconnect_failures"] += 1
        self._outage_failed_attempts += 1
        self._last_failure_reason = reason

    def _offline_summary_due_locked(self, now: float) -> bool:
        if self._outage_started_at is None or now - self._outage_started_at < OFFLINE_SUMMARY_INTERVAL_SECONDS:
            return False
        if self._last_offline_summary_at is not None and now - self._last_offline_summary_at < OFFLINE_SUMMARY_INTERVAL_SECONDS:
            return False
        self._last_offline_summary_at = now
        return True

    def _downtime_locked(self, now: float) -> float | None:
        return now - self._outage_started_at if self._outage_started_at is not None else None

    def _record_transition_locked(
        self, now: float, transition: UpstreamTransition, message: str
    ) -> None:
        self._record_event_locked(now, f"upstream_{transition.kind.value}", message, transition.details())

    def _record_event_locked(
        self, now: float, kind: str, message: str, details: dict[str, Any]
    ) -> None:
        self._events.append(RuntimeEvent(now, kind, message, details))

    def _availability_locked(self, now: float, window_seconds: float) -> float | None:
        """Estimate ONLINE share from retained state transitions."""
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
