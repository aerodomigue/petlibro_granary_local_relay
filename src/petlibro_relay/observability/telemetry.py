"""Thread-safe transition-aware runtime telemetry for relay observability.

Split in two, mirroring the runtime:

* `DeviceTelemetry` - one per device. Owns that device's upstream state
  machine, counters, session history and availability. Devices fail
  independently, so these must never be shared: one feeder's cloud outage
  says nothing about another's.
* `RelayTelemetry` - process-wide. Owns uptime, the local broker's state and
  the global event timeline, and aggregates the per-device views for the
  dashboard header.
"""

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
SESSION_HISTORY_LIMIT = 500
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
    device_id: str
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
    device_id: str | None
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-compatible event data."""
        return asdict(self)


class DeviceTelemetry:
    """One device's upstream metrics, without changing MQTT behavior.

    This class owns the semantic state machine because Paho may invoke
    ``on_disconnect`` for a half-open connect attempt as well as for a real
    online session. A callback is therefore not intrinsically a session loss.
    """

    def __init__(
        self,
        device_id: str,
        sink: "RelayTelemetry | None" = None,
        started_at: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialize telemetry for one device.

        Args:
            device_id: Device these metrics describe.
            sink: Relay-wide telemetry to mirror events into, so the global
                timeline stays complete without this class owning it.
            started_at: When observation began; defaults to now.
            clock: Time source, injectable for tests.
        """
        self._device_id = device_id
        self._sink = sink
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._started_at = started_at if started_at is not None else self._clock()
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
        self._local_online = False
        self._local_last_seen_at: float | None = None
        self._state_history: collections.deque[tuple[float, bool]] = collections.deque(
            maxlen=EVENT_HISTORY_LIMIT
        )
        self._session_durations: collections.deque[float] = collections.deque(
            maxlen=SESSION_HISTORY_LIMIT
        )
        self._counters: collections.Counter[str] = collections.Counter()

    @property
    def device_id(self) -> str:
        """Return the device these metrics belong to."""
        return self._device_id

    @property
    def upstream_state(self) -> UpstreamState:
        """Return the current semantic upstream state."""
        with self._lock:
            return self._upstream_state

    @property
    def local_online(self) -> bool:
        """Return whether the feeder currently holds a local session."""
        with self._lock:
            return self._local_online

    def local_session_opened(self) -> None:
        """Record the feeder opening a connection through the capture proxy."""
        with self._lock:
            self._local_online = True
            self._local_last_seen_at = self._clock()
            self._counters["local_sessions"] += 1

    def local_session_closed(self) -> None:
        """Record the feeder's last local connection going away."""
        with self._lock:
            self._local_online = False
            self._local_last_seen_at = self._clock()

    def upstream_connect_attempt(self) -> UpstreamTransition:
        """Record an expected MQTT CONNECT attempt, not an ONLINE session."""
        with self._lock:
            now = self._clock()
            before = self._upstream_state
            self._begin_outage_locked(now)
            self._outage_attempts += 1
            self._counters["connect_attempts"] += 1
            self._upstream_state = UpstreamState.MQTT_CONNECTING
            transition = self._transition_locked(
                UpstreamTransitionKind.CONNECT_ATTEMPT, before, attempt=self._outage_attempts
            )
        self._publish(now, transition, "Upstream MQTT CONNECT attempt")
        return transition

    def upstream_online(self) -> UpstreamTransition:
        """Mark online only after a successful CONNACK and close any outage."""
        with self._lock:
            now = self._clock()
            before = self._upstream_state
            self._begin_outage_locked(now)
            downtime = (
                now - self._outage_started_at if self._outage_started_at is not None else None
            )
            failed_attempts = self._outage_failed_attempts
            attempt = self._outage_attempts
            restored = self._outage_had_session_loss or failed_attempts > 0
            self._upstream_state = UpstreamState.ONLINE
            self._last_connack_at = now
            self._last_online_started_at = now
            self._session_number += 1
            self._counters["connack_success"] += 1
            self._state_history.append((now, True))
            transition = self._transition_locked(
                UpstreamTransitionKind.RESTORED if restored else UpstreamTransitionKind.ONLINE,
                before,
                attempt=attempt,
                downtime_seconds=downtime,
                failed_attempts=failed_attempts,
            )
            self._clear_outage_locked()
        self._publish(now, transition, "UPSTREAM restored" if restored else "UPSTREAM online")
        return transition

    def upstream_refused(self, reason_code: str) -> UpstreamTransition:
        """Record a broker-level CONNACK rejection as a failed retry."""
        with self._lock:
            now = self._clock()
            before = self._upstream_state
            self._ensure_attempt_for_failure_locked(now)
            self._counters["connack_refused"] += 1
            self._register_retry_failure_locked(reason_code)
            self._upstream_state = UpstreamState.DISCONNECTED
            transition = self._transition_locked(
                UpstreamTransitionKind.CONNACK_REFUSED,
                before,
                attempt=self._outage_attempts,
                reason_code=reason_code,
                downtime_seconds=self._downtime_locked(now),
                failed_attempts=self._outage_failed_attempts,
                offline_summary_due=self._offline_summary_due_locked(now),
            )
        self._publish(now, transition, "Upstream CONNACK refused")
        return transition

    def upstream_connect_failed(
        self, reason: str = "TCP/DNS connect failure"
    ) -> UpstreamTransition:
        """Record a TCP/DNS connection failure when Paho calls on_connect_fail."""
        with self._lock:
            now = self._clock()
            before = self._upstream_state
            self._ensure_attempt_for_failure_locked(now)
            self._counters["tcp_connect_failures"] += 1
            self._register_retry_failure_locked(reason)
            self._upstream_state = UpstreamState.DISCONNECTED
            transition = self._transition_locked(
                UpstreamTransitionKind.TCP_CONNECT_FAILED,
                before,
                attempt=self._outage_attempts,
                reason=reason,
                downtime_seconds=self._downtime_locked(now),
                failed_attempts=self._outage_failed_attempts,
                offline_summary_due=self._offline_summary_due_locked(now),
            )
        self._publish(now, transition, "Upstream TCP connect failed")
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
                    now - self._last_online_started_at
                    if self._last_online_started_at is not None
                    else None
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
                transition = self._transition_locked(
                    UpstreamTransitionKind.SESSION_LOST,
                    before,
                    attempt=0,
                    reason=reason,
                    disconnect_flags=disconnect_flags,
                    session_duration_seconds=session_duration,
                )
                message = "UPSTREAM lost"
            else:
                self._ensure_attempt_for_failure_locked(now)
                if before is UpstreamState.MQTT_CONNECTING:
                    self._counters["connack_timeouts"] += 1
                self._register_retry_failure_locked(reason)
                self._last_disconnect_at = now
                self._upstream_state = UpstreamState.DISCONNECTED
                transition = self._transition_locked(
                    UpstreamTransitionKind.RETRY_FAILED,
                    before,
                    attempt=self._outage_attempts,
                    reason=reason,
                    disconnect_flags=disconnect_flags,
                    downtime_seconds=self._downtime_locked(now),
                    failed_attempts=self._outage_failed_attempts,
                    offline_summary_due=self._offline_summary_due_locked(now),
                )
                message = "Upstream reconnect failed"
        self._publish(now, transition, message)
        return transition

    def increment(self, counter: str, amount: int = 1) -> None:
        """Increase a named operation counter for this device only."""
        with self._lock:
            self._counters[counter] += amount

    def snapshot(self) -> dict[str, Any]:
        """Return one consistent, JSON-ready view of this device's metrics."""
        now = self._clock()
        with self._lock:
            current_duration = (
                now - self._last_online_started_at
                if self._last_online_started_at is not None
                else None
            )
            durations = list(self._session_durations)
            return {
                "device_id": self._device_id,
                "local": {"online": self._local_online, "last_seen_at": self._local_last_seen_at},
                "upstream": {
                    "state": self._upstream_state.name,
                    "last_connack_0": self._last_connack_at,
                    "last_disconnect": self._last_disconnect_at,
                    "current_online_duration_seconds": current_duration,
                    "previous_session_duration_seconds": durations[-1] if durations else None,
                    "average_session_duration_seconds": (
                        sum(durations) / len(durations) if durations else None
                    ),
                    "minimum_session_duration_seconds": min(durations) if durations else None,
                    "maximum_session_duration_seconds": max(durations) if durations else None,
                    "counters": dict(self._counters),
                    "outage": {
                        "started_at": self._outage_started_at,
                        "downtime_seconds": (
                            now - self._outage_started_at
                            if self._outage_started_at is not None
                            else None
                        ),
                        "attempts": self._outage_attempts,
                        "failed_attempts": self._outage_failed_attempts,
                        "last_reason": self._last_failure_reason,
                    },
                    "availability": {
                        "15m": self._availability_locked(now, 15 * 60),
                        "1h": self._availability_locked(now, 60 * 60),
                        "24h": self._availability_locked(now, 24 * 60 * 60),
                    },
                },
            }

    # -- internals ---------------------------------------------------------------

    def _publish(self, now: float, transition: UpstreamTransition, message: str) -> None:
        """Mirror a transition into the relay-wide timeline, outside our lock."""
        if self._sink is None:
            return
        self._sink.record_event(
            f"upstream_{transition.kind.value}",
            message,
            device_id=self._device_id,
            timestamp=now,
            details=transition.details(),
        )

    def _transition_locked(
        self,
        kind: UpstreamTransitionKind,
        before: UpstreamState,
        attempt: int,
        **fields: Any,
    ) -> UpstreamTransition:
        return UpstreamTransition(
            kind=kind,
            device_id=self._device_id,
            state_before=before.name,
            state_after=self._upstream_state.name,
            attempt=attempt,
            **fields,
        )

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

    def _register_retry_failure_locked(self, reason: str) -> None:
        self._counters["reconnect_failures"] += 1
        self._outage_failed_attempts += 1
        self._last_failure_reason = reason

    def _offline_summary_due_locked(self, now: float) -> bool:
        if (
            self._outage_started_at is None
            or now - self._outage_started_at < OFFLINE_SUMMARY_INTERVAL_SECONDS
        ):
            return False
        if (
            self._last_offline_summary_at is not None
            and now - self._last_offline_summary_at < OFFLINE_SUMMARY_INTERVAL_SECONDS
        ):
            return False
        self._last_offline_summary_at = now
        return True

    def _downtime_locked(self, now: float) -> float | None:
        return now - self._outage_started_at if self._outage_started_at is not None else None

    def _availability_locked(self, now: float, window_seconds: float) -> float | None:
        """Estimate ONLINE share from retained state transitions."""
        window_start = max(self._started_at, now - window_seconds)
        transitions = [
            (timestamp, online) for timestamp, online in self._state_history if timestamp >= window_start
        ]
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


class RelayTelemetry:
    """Process-wide telemetry and the owner of every device's metrics."""

    def __init__(
        self, started_at: float | None = None, clock: Callable[[], float] | None = None
    ) -> None:
        """Initialize relay-wide telemetry.

        Args:
            started_at: Process start time; defaults to now.
            clock: Time source, injectable for tests.
        """
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._started_at = started_at if started_at is not None else self._clock()
        self._local_connected = False
        self._counters: collections.Counter[str] = collections.Counter()
        self._events: collections.deque[RuntimeEvent] = collections.deque(maxlen=EVENT_HISTORY_LIMIT)
        self._devices: dict[str, DeviceTelemetry] = {}

    def device(self, device_id: str) -> DeviceTelemetry:
        """Return (creating on first use) one device's telemetry."""
        with self._lock:
            telemetry = self._devices.get(device_id)
            if telemetry is None:
                telemetry = DeviceTelemetry(device_id, sink=self, clock=self._clock)
                self._devices[device_id] = telemetry
            return telemetry

    def forget_device(self, device_id: str) -> None:
        """Drop a device's metrics once it is no longer bridged."""
        with self._lock:
            self._devices.pop(device_id, None)

    def device_snapshots(self) -> list[dict[str, Any]]:
        """Return every device's metrics, ordered for stable rendering."""
        with self._lock:
            devices = list(self._devices.values())
        return [device.snapshot() for device in sorted(devices, key=lambda item: item.device_id)]

    def record_event(
        self,
        kind: str,
        message: str,
        device_id: str | None = None,
        timestamp: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append a bounded dashboard event.

        `details` is passed as a mapping rather than keyword arguments so an
        event field can share a name with a parameter here without colliding.
        """
        with self._lock:
            self._events.append(
                RuntimeEvent(
                    timestamp if timestamp is not None else self._clock(),
                    kind,
                    message,
                    device_id,
                    details or {},
                )
            )

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

    def increment(self, counter: str, amount: int = 1) -> None:
        """Increase a relay-wide counter not attributable to one device."""
        with self._lock:
            self._counters[counter] += amount

    def snapshot(self) -> dict[str, Any]:
        """Return relay-wide facts plus an aggregate over all devices."""
        now = self._clock()
        device_snapshots = self.device_snapshots()
        with self._lock:
            local_connected = self._local_connected
            counters = dict(self._counters)
        return {
            "started_at": self._started_at,
            "uptime_seconds": now - self._started_at,
            "local_mqtt": {"connected": local_connected},
            "counters": counters,
            "devices": _aggregate_devices(device_snapshots),
        }

    def events(self, limit: int = 100, device_id: str | None = None) -> list[dict[str, Any]]:
        """Return newest retained events, optionally for one device only."""
        safe_limit = max(1, min(limit, EVENT_HISTORY_LIMIT))
        with self._lock:
            events = [
                event.as_dict()
                for event in self._events
                if device_id is None or event.device_id == device_id
            ]
        return events[-safe_limit:]


def _aggregate_devices(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize per-device states into the counts the header shows."""
    upstream_states = [str(item["upstream"]["state"]) for item in snapshots]
    return {
        "known": len(snapshots),
        "local_online": sum(1 for item in snapshots if item["local"]["online"]),
        "upstream_online": sum(1 for state in upstream_states if state == "ONLINE"),
        "upstream_degraded": sum(1 for state in upstream_states if state != "ONLINE"),
    }
