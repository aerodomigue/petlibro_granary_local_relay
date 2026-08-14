"""Restore persisted schedules when a feeder establishes a local MQTT session."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol

from .device_presence import DevicePresenceTracker
from .sound_switch_control import ControlAckTimeoutError, ControlError, ScheduleResyncResult

_LOGGER = logging.getLogger(__name__)

SCHEDULE_RESYNC_DEBOUNCE_SECONDS = 1.5


class PersistedScheduleResync(Protocol):
    """The narrow controller seam used by the local-presence lifecycle."""

    def resync_persisted_schedules(self, device_id: str) -> ScheduleResyncResult | None:
        """Send one persisted schedule snapshot to the local feeder."""


TimerFactory = Callable[[float, Callable[[], None]], threading.Timer]


class ScheduleResyncCoordinator:
    """Debounce one local-only schedule restoration per feeder connection.

    Presence remains the source of truth.  The coordinator observes its
    monotonically increasing connection generation, so repeated lifecycle
    reconciliations and UI polls cannot cause another send.  A new local MQTT
    connection receives a new generation and therefore one new resync after
    the short stability delay.
    """

    def __init__(
        self,
        controller: PersistedScheduleResync,
        presence: DevicePresenceTracker,
        debounce_seconds: float = SCHEDULE_RESYNC_DEBOUNCE_SECONDS,
        timer_factory: TimerFactory = threading.Timer,
    ) -> None:
        """Initialize the non-blocking lifecycle observer.

        Args:
            controller: Sends the correlated local-only snapshot and waits for ACK.
            presence: Source of local MQTT connectivity and connection generations.
            debounce_seconds: Delay after CONNECT before publishing a snapshot.
            timer_factory: Injectable timer constructor for deterministic tests.
        """
        self._controller = controller
        self._presence = presence
        self._debounce_seconds = debounce_seconds
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._generations: dict[str, int] = {}
        self._timers: dict[str, threading.Timer] = {}

    def device_online(self, device_id: str, connection_generation: int) -> None:
        """Schedule exactly one restoration for a real local MQTT connection."""
        with self._lock:
            if self._generations.get(device_id) == connection_generation:
                return
            previous = self._timers.pop(device_id, None)
            if previous is not None:
                previous.cancel()
            self._generations[device_id] = connection_generation
            timer = self._timer_factory(
                self._debounce_seconds,
                lambda: self._resync_if_still_current(device_id, connection_generation),
            )
            timer.daemon = True
            self._timers[device_id] = timer
            timer.start()

    def stop(self) -> None:
        """Cancel delayed work before the relay closes its stores and clients."""
        with self._lock:
            timers = list(self._timers.values())
            self._timers.clear()
        for timer in timers:
            timer.cancel()

    def _resync_if_still_current(self, device_id: str, connection_generation: int) -> None:
        with self._lock:
            if self._generations.get(device_id) != connection_generation:
                return
            self._timers.pop(device_id, None)
        record = self._presence.record(device_id)
        if (
            record is None
            or not record.connected
            or record.connection_generation != connection_generation
        ):
            _LOGGER.debug(
                "SCHEDULE RESYNC SKIP device=%s reason=session_not_stable generation=%d",
                device_id,
                connection_generation,
            )
            return
        try:
            self._controller.resync_persisted_schedules(device_id)
        except ControlAckTimeoutError:
            _LOGGER.warning("SCHEDULE RESYNC TIMEOUT device=%s", device_id)
        except ControlError as error:
            _LOGGER.warning("SCHEDULE RESYNC FAILED device=%s reason=%s", device_id, error)
