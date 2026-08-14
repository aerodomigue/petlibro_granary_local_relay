"""Lifecycle-only tests for persisted schedule restoration after local reconnects."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from petlibro_relay.device_presence import DevicePresenceTracker
from petlibro_relay.schedule_resync import ScheduleResyncCoordinator
from petlibro_relay.sound_switch_control import ControlAckTimeoutError, ScheduleResyncResult

DEVICE_A = "DEVICE-A"


class ManualTimer(threading.Timer):
    """A timer whose callback tests trigger explicitly without sleeping."""

    def start(self) -> None:
        """Leave callback execution under test control."""

    def fire(self) -> None:
        """Run the callback once unless a newer connection cancelled this timer."""
        if self.finished.is_set():
            return
        self.function(*self.args, **self.kwargs)


class RecordingResyncController:
    """Records lifecycle requests without using a feeder or MQTT broker."""

    def __init__(self, failure: ControlAckTimeoutError | None = None) -> None:
        self.calls: list[str] = []
        self.failure = failure

    def resync_persisted_schedules(self, device_id: str) -> ScheduleResyncResult | None:
        """Record a synthetic successful (or timed out) persisted resync."""
        self.calls.append(device_id)
        if self.failure is not None:
            raise self.failure
        return ScheduleResyncResult(device_id, plans_total=2, cloud_plans=1, local_plans=1)


def test_reconnect_schedules_exactly_one_stable_resync() -> None:
    """Repeated supervisor passes for one connection never create duplicate sends."""
    presence = DevicePresenceTracker()
    presence.session_opened(DEVICE_A)
    record = presence.record(DEVICE_A)
    assert record is not None
    timers: list[ManualTimer] = []

    def timer_factory(delay: float, callback: Callable[[], None]) -> threading.Timer:
        timer = ManualTimer(delay, callback)
        timers.append(timer)
        return timer

    controller = RecordingResyncController()
    coordinator = ScheduleResyncCoordinator(controller, presence, timer_factory=timer_factory)

    coordinator.device_online(DEVICE_A, record.connection_generation)
    coordinator.device_online(DEVICE_A, record.connection_generation)
    assert len(timers) == 1

    timers[0].fire()
    coordinator.device_online(DEVICE_A, record.connection_generation)

    assert controller.calls == [DEVICE_A]
    assert len(timers) == 1


def test_connection_flap_debounces_to_the_latest_generation() -> None:
    """A superseded reconnect timer cannot publish after the next session opens."""
    presence = DevicePresenceTracker()
    presence.session_opened(DEVICE_A)
    first = presence.record(DEVICE_A)
    assert first is not None
    timers: list[ManualTimer] = []

    def timer_factory(delay: float, callback: Callable[[], None]) -> threading.Timer:
        timer = ManualTimer(delay, callback)
        timers.append(timer)
        return timer

    controller = RecordingResyncController()
    coordinator = ScheduleResyncCoordinator(controller, presence, timer_factory=timer_factory)
    coordinator.device_online(DEVICE_A, first.connection_generation)

    presence.session_closed(DEVICE_A)
    presence.session_opened(DEVICE_A)
    second = presence.record(DEVICE_A)
    assert second is not None
    coordinator.device_online(DEVICE_A, second.connection_generation)

    timers[0].fire()
    timers[1].fire()

    assert controller.calls == [DEVICE_A]


def test_timeout_is_observable_without_scheduling_another_send(caplog: logging.LogCaptureFixture) -> None:
    """A missing ACK leaves durable state alone and produces one explicit timeout log."""
    presence = DevicePresenceTracker()
    presence.session_opened(DEVICE_A)
    record = presence.record(DEVICE_A)
    assert record is not None
    timers: list[ManualTimer] = []

    def timer_factory(delay: float, callback: Callable[[], None]) -> threading.Timer:
        timer = ManualTimer(delay, callback)
        timers.append(timer)
        return timer

    controller = RecordingResyncController(ControlAckTimeoutError("Device acknowledgement timeout"))
    coordinator = ScheduleResyncCoordinator(controller, presence, timer_factory=timer_factory)

    with caplog.at_level(logging.WARNING):
        coordinator.device_online(DEVICE_A, record.connection_generation)
        timers[0].fire()

    assert controller.calls == [DEVICE_A]
    assert "SCHEDULE RESYNC TIMEOUT device=DEVICE-A" in caplog.text
