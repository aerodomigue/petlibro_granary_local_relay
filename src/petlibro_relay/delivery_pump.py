"""Background delivery pump with fair, rate-limited upstream replay."""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import paho.mqtt.client as mqtt

from .message_queue import MessageQueue, QueuedMessage
from .observability.telemetry import DeviceTelemetry
from .replay_policy import extract_command

_LOGGER = logging.getLogger(__name__)

IDLE_POLL_INTERVAL_SECONDS = 1.0
PUBLISH_RETRY_INTERVAL_SECONDS = 2.0
PUBLISH_CONFIRM_TIMEOUT_SECONDS = 10.0
STOP_JOIN_TIMEOUT_SECONDS = 5.0
LEGACY_DEVICE_BATCH_SIZE = 50
MIN_SCHEDULER_WAIT_SECONDS = 0.01


@dataclass(frozen=True, slots=True)
class PumpTarget:
    """One device's destination for a direction."""

    device_id: str
    client: mqtt.Client
    telemetry: DeviceTelemetry
    replay_ready: bool = True


@dataclass(slots=True)
class ReplayProgress:
    """One device's current controlled replay window."""

    scheduled_at: float
    ready_at: float
    sent: int = 0
    expired: int = 0
    started: bool = False


class DeliveryPump:
    """Drain a queue direction for all bridged devices.

    Only device -> cloud delivery receives controlled replay parameters. New
    live rows are selected ahead of the pre-existing backlog and are never
    throttled, while old rows are sent one at a time in a rotating device
    order after the upstream session has had time to settle.
    """

    def __init__(
        self,
        direction: str,
        queue: MessageQueue,
        targets: Callable[[], list[PumpTarget]],
        is_cloud_to_device: bool,
        replay_rate_per_device: float | None = None,
        replay_rate_global: float | None = None,
        replay_start_delay_seconds: float = 0.0,
        replay_jitter: float = 0.0,
        clock: Callable[[], float] | None = None,
        random_source: Callable[[], float] | None = None,
    ) -> None:
        """Initialize the shared delivery scheduler.

        Args:
            direction: Logical queue direction.
            queue: Shared durable message queue.
            targets: Current destinations, one per bridged device.
            is_cloud_to_device: True for cloud commands; they retain legacy
                FIFO draining and are never rate-limited here.
            replay_rate_per_device: Maximum backlog messages per second for
                one device. Both rate arguments must be set to enable control.
            replay_rate_global: Maximum backlog messages per second across
                all devices.
            replay_start_delay_seconds: Delay after replay is first observed.
            replay_jitter: Maximum fractional extra delay applied to replay
                intervals; it never permits a rate above the configured cap.
            clock: Injectable wall clock for deterministic scheduler tests.
            random_source: Injectable source returning values in [0, 1].
        """
        if (replay_rate_per_device is None) != (replay_rate_global is None):
            raise ValueError("Replay per-device and global rates must be configured together")
        if replay_rate_per_device is not None and replay_rate_per_device <= 0:
            raise ValueError("Replay per-device rate must be greater than zero")
        if replay_rate_global is not None and replay_rate_global <= 0:
            raise ValueError("Replay global rate must be greater than zero")
        if replay_start_delay_seconds < 0:
            raise ValueError("Replay start delay must not be negative")
        if not 0 <= replay_jitter <= 1:
            raise ValueError("Replay jitter must be between zero and one")

        self._direction = direction
        self._queue = queue
        self._targets = targets
        self._is_cloud_to_device = is_cloud_to_device
        self._replay_rate_per_device = replay_rate_per_device
        self._replay_rate_global = replay_rate_global
        self._replay_start_delay_seconds = replay_start_delay_seconds
        self._replay_jitter = replay_jitter
        self._clock = clock or time.time
        self._random_source = random_source or random.random
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"pump-{direction}", daemon=True)
        self._next_device_send_at: dict[str, float] = {}
        self._next_global_send_at = 0.0
        self._replay_progress: dict[str, ReplayProgress] = {}
        self._round_robin_offset = 0
        self._offline_devices: set[str] = set()

    @property
    def _controlled_replay_enabled(self) -> bool:
        """Return whether this pump controls device-to-cloud backlog replay."""
        return not self._is_cloud_to_device and self._replay_rate_per_device is not None

    def start(self) -> None:
        """Start the background scheduler thread."""
        self._thread.start()

    def stop(self) -> None:
        """Signal the scheduler to stop and wait for its thread to exit."""
        self._stop_event.set()
        self._thread.join(timeout=STOP_JOIN_TIMEOUT_SECONDS)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            delivered_anything = self._run_once()
            if not delivered_anything:
                self._stop_event.wait(self._next_wait_seconds())

    def _run_once(self) -> bool:
        """Run one fair scheduling pass. Kept separate for deterministic tests."""
        targets = self._rotated_targets()
        known_device_ids = {target.device_id for target in targets}
        for device_id in tuple(self._replay_progress):
            if device_id not in known_device_ids:
                self._replay_progress.pop(device_id, None)
                self._next_device_send_at.pop(device_id, None)

        delivered_anything = False
        for target in targets:
            if self._stop_event.is_set():
                return delivered_anything
            delivered_anything |= self._drain_device(target)
        return delivered_anything

    def _rotated_targets(self) -> list[PumpTarget]:
        """Return targets rotated by one start position per scheduling pass."""
        targets = self._targets()
        if not targets:
            return []
        start = self._round_robin_offset % len(targets)
        self._round_robin_offset = (self._round_robin_offset + 1) % len(targets)
        return targets[start:] + targets[:start]

    def _drain_device(self, target: PumpTarget) -> bool:
        """Deliver one controlled replay row, or a legacy bounded FIFO batch."""
        if not target.client.is_connected():
            if target.device_id not in self._offline_devices:
                self._queue.demote_live_messages(target.device_id, self._direction)
                self._offline_devices.add(target.device_id)
            self._pause_replay(target.device_id)
            return False
        self._offline_devices.discard(target.device_id)

        delivered = False
        batch_size = 1 if self._controlled_replay_enabled else LEGACY_DEVICE_BATCH_SIZE
        for _ in range(batch_size):
            if self._stop_event.is_set():
                return delivered
            message = self._queue.peek_oldest(
                target.device_id,
                self._direction,
                prioritize_live=self._controlled_replay_enabled,
            )
            if message is None:
                self._complete_replay_if_needed(target)
                return delivered

            if self._is_expired(message):
                self._queue.remove(message.id)
                if self._controlled_replay_enabled and not message.is_live:
                    self._record_expired_replay(target)
                else:
                    target.telemetry.increment("queue_expired")
                delivered = True
                continue

            if self._controlled_replay_enabled and not message.is_live:
                if not self._can_send_replay(target):
                    target.telemetry.increment("replay_rate_limited")
                    return delivered

            if not self._publish_confirmed(target, message.topic, message.payload, message.qos):
                self._stop_event.wait(PUBLISH_RETRY_INTERVAL_SECONDS)
                return delivered

            self._queue.remove(message.id)
            target.telemetry.increment(f"queue_delivered_{self._direction}")
            if self._controlled_replay_enabled and not message.is_live:
                self._record_replay_sent(target)
            delivered = True
        return delivered

    def _can_send_replay(self, target: PumpTarget) -> bool:
        """Return whether a backlog row has passed settle and both rate budgets."""
        if not target.replay_ready:
            return False
        now = self._clock()
        progress = self._replay_progress.get(target.device_id)
        if progress is None:
            backlog = self._queue.backlog_count(target.device_id, self._direction)
            progress = ReplayProgress(
                scheduled_at=now,
                ready_at=now + self._replay_start_delay_seconds,
            )
            self._replay_progress[target.device_id] = progress
            _LOGGER.info(
                "Replay scheduled device=%s backlog=%d start_delay=%.2fs",
                target.device_id,
                backlog,
                self._replay_start_delay_seconds,
            )
        if now < progress.ready_at:
            return False
        return now >= self._next_device_send_at.get(target.device_id, 0.0) and now >= self._next_global_send_at

    def _record_replay_sent(self, target: PumpTarget) -> None:
        """Account for a confirmed replay publish and advance both rate budgets."""
        assert self._replay_rate_per_device is not None
        assert self._replay_rate_global is not None
        now = self._clock()
        progress = self._replay_progress[target.device_id]
        if not progress.started:
            progress.started = True
            _LOGGER.info(
                "Replay started device=%s rate_limit=%.2f/s global_rate_limit=%.2f/s",
                target.device_id,
                self._replay_rate_per_device,
                self._replay_rate_global,
            )
        progress.sent += 1
        target.telemetry.increment("replay_sent")
        self._next_device_send_at[target.device_id] = now + self._jittered_interval(
            self._replay_rate_per_device
        )
        self._next_global_send_at = now + self._jittered_interval(self._replay_rate_global)
        self._complete_replay_if_needed(target)

    def _record_expired_replay(self, target: PumpTarget) -> None:
        """Record a stale backlog row that was removed without publishing."""
        progress = self._replay_progress.get(target.device_id)
        if progress is not None:
            progress.expired += 1
        target.telemetry.increment("replay_dropped_expired")
        self._complete_replay_if_needed(target)

    def _complete_replay_if_needed(self, target: PumpTarget) -> None:
        """Log one completion event once a scheduled device has no backlog left."""
        progress = self._replay_progress.get(target.device_id)
        if progress is None or self._queue.backlog_count(target.device_id, self._direction) != 0:
            return
        duration = self._clock() - progress.scheduled_at
        _LOGGER.info(
            "Replay complete device=%s sent=%d expired=%d duration=%.2fs",
            target.device_id,
            progress.sent,
            progress.expired,
            duration,
        )
        self._replay_progress.pop(target.device_id, None)
        self._next_device_send_at.pop(target.device_id, None)

    def _pause_replay(self, device_id: str) -> None:
        """Forget timing state when a device loses its upstream session."""
        self._replay_progress.pop(device_id, None)
        self._next_device_send_at.pop(device_id, None)

    def _jittered_interval(self, rate_per_second: float) -> float:
        """Return a jittered interval that never exceeds the configured rate."""
        base_interval = 1.0 / rate_per_second
        if self._replay_jitter == 0:
            return base_interval
        return base_interval * (1.0 + self._random_source() * self._replay_jitter)

    def _next_wait_seconds(self) -> float:
        """Return a bounded wait until the next controlled scheduler deadline."""
        if not self._controlled_replay_enabled:
            return IDLE_POLL_INTERVAL_SECONDS
        now = self._clock()
        deadlines: list[float] = []
        if self._next_global_send_at > now:
            deadlines.append(self._next_global_send_at)
        deadlines.extend(deadline for deadline in self._next_device_send_at.values() if deadline > now)
        deadlines.extend(progress.ready_at for progress in self._replay_progress.values())
        future = [deadline - now for deadline in deadlines if deadline > now]
        if not future:
            return IDLE_POLL_INTERVAL_SECONDS
        return max(MIN_SCHEDULER_WAIT_SECONDS, min(IDLE_POLL_INTERVAL_SECONDS, min(future)))

    def _is_expired(self, message: QueuedMessage) -> bool:
        """Return True if this row's insertion-time expiry has elapsed."""
        if message.max_age_seconds is None:
            return False
        age_seconds = self._clock() - message.created_at
        if age_seconds <= message.max_age_seconds:
            return False
        _LOGGER.warning(
            "Dropping stale %s message on %s (cmd=%s, age=%.1fs > %.1fs) rather than acting on it late",
            self._direction,
            message.topic,
            extract_command(message.payload),
            age_seconds,
            message.max_age_seconds,
        )
        return True

    def _publish_confirmed(
        self, target: PumpTarget, topic: str, payload: bytes, qos: int
    ) -> bool:
        """Publish and wait for Paho's send confirmation before deleting a row."""
        message_info = target.client.publish(topic, payload, qos=qos)
        if message_info.rc != mqtt.MQTT_ERR_SUCCESS:
            _LOGGER.warning(
                "Publish rejected for %s/%s (topic=%s, rc=%s), will retry",
                target.device_id,
                self._direction,
                topic,
                message_info.rc,
            )
            return False
        try:
            message_info.wait_for_publish(timeout=PUBLISH_CONFIRM_TIMEOUT_SECONDS)
        except (ValueError, RuntimeError) as error:
            _LOGGER.warning(
                "Publish not confirmed for %s/%s (topic=%s): %s, will retry",
                target.device_id,
                self._direction,
                topic,
                error,
            )
            return False
        if not message_info.is_published():
            _LOGGER.warning(
                "Publish timed out unconfirmed for %s/%s (topic=%s), will retry",
                target.device_id,
                self._direction,
                topic,
            )
            return False
        return True
