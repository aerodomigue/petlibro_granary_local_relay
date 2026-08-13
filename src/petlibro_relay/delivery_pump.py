"""Background delivery pump that drains one queue direction for every device.

One pump per direction, not per device: N feeders would otherwise mean 2N
threads doing nothing most of the time. The pump instead round-robins over the
devices currently bridged, draining a bounded batch for each before moving on,
so a device with a large backlog cannot starve the others.

Isolation still holds. Each device's messages are read with its own id, and
published onto its own destination client - for the device -> cloud direction
that is its own authenticated cloud session, so a message can never leave
under another device's credentials. A device whose destination is offline is
simply skipped; the rest keep flowing.
"""

from __future__ import annotations

import logging
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
# Messages drained for one device before yielding to the next. Bounds how long
# a single busy device can hold the shared thread.
DEVICE_BATCH_SIZE = 50


@dataclass(frozen=True, slots=True)
class PumpTarget:
    """One device's destination for a direction."""

    device_id: str
    client: mqtt.Client
    telemetry: DeviceTelemetry


class DeliveryPump:
    """Drains one direction of a `MessageQueue` for every bridged device.

    Runs in its own thread so a slow or disconnected destination never blocks
    message ingestion on the other side of the bridge. When a device
    reconnects after an outage, its backlog resumes draining automatically and
    the pump logs how many messages it is replaying (the "resync") for that
    device specifically.

    Messages whose replay policy has expired are dropped rather than
    delivered late - see `replay_policy` for why that matters in the
    cloud -> device direction.
    """

    def __init__(
        self,
        direction: str,
        queue: MessageQueue,
        targets: Callable[[], list[PumpTarget]],
        is_cloud_to_device: bool,
    ) -> None:
        """Initialize the pump.

        Args:
            direction: Logical queue name this pump drains (must match the
                `direction` used when messages were enqueued).
            queue: Shared durable queue.
            targets: Returns the currently bridged devices and where to
                publish each one's messages. Called every cycle, so devices
                added or removed at runtime are picked up without a restart.
            is_cloud_to_device: Whether this direction carries commands that
                act on the physical world when delivered, and therefore need
                replay policies applied.
        """
        self._direction = direction
        self._queue = queue
        self._targets = targets
        self._is_cloud_to_device = is_cloud_to_device
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"pump-{direction}", daemon=True)
        self._had_backlog_during_outage: dict[str, bool] = {}

    def start(self) -> None:
        """Start the background delivery thread."""
        self._thread.start()

    def stop(self) -> None:
        """Signal the pump to stop and wait for its thread to exit."""
        self._stop_event.set()
        self._thread.join(timeout=STOP_JOIN_TIMEOUT_SECONDS)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            delivered_anything = False
            for target in self._targets():
                if self._stop_event.is_set():
                    return
                delivered_anything |= self._drain_device(target)
            if not delivered_anything:
                self._stop_event.wait(IDLE_POLL_INTERVAL_SECONDS)

    def _drain_device(self, target: PumpTarget) -> bool:
        """Deliver up to one batch for a device. Returns whether anything moved."""
        if not target.client.is_connected():
            if self._queue.count(target.device_id, self._direction) > 0:
                self._had_backlog_during_outage[target.device_id] = True
            return False

        self._log_resync_if_needed(target)

        delivered = False
        for _ in range(DEVICE_BATCH_SIZE):
            if self._stop_event.is_set():
                return delivered
            message = self._queue.peek_oldest(target.device_id, self._direction)
            if message is None:
                return delivered

            if self._is_expired(message):
                self._queue.remove(message.id)
                target.telemetry.increment("queue_expired")
                delivered = True
                continue

            if not self._publish_confirmed(target, message.topic, message.payload, message.qos):
                self._stop_event.wait(PUBLISH_RETRY_INTERVAL_SECONDS)
                return delivered

            self._queue.remove(message.id)
            target.telemetry.increment(f"queue_delivered_{self._direction}")
            delivered = True
        return delivered

    def _is_expired(self, message: QueuedMessage) -> bool:
        """Return True if this row's insertion-time expiry has elapsed."""
        if message.max_age_seconds is None:
            return False
        age_seconds = time.time() - message.created_at
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
        """Publish and wait for the client to confirm the packet actually went out.

        `publish()` returning MQTT_ERR_SUCCESS only means paho accepted the
        message into its outgoing buffer; the socket write can still fail.
        Waiting for publication before removing the message from the durable
        queue is what makes a crash mid-send replay rather than lose it.
        """
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

    def _log_resync_if_needed(self, target: PumpTarget) -> None:
        if not self._had_backlog_during_outage.get(target.device_id):
            return
        pending = self._queue.count(target.device_id, self._direction)
        if pending:
            _LOGGER.info(
                "Destination for %s/%s is back online, replaying %d backlogged message(s)",
                target.device_id,
                self._direction,
                pending,
            )
            target.telemetry.increment(f"queue_replayed_{self._direction}", pending)
        self._had_backlog_during_outage[target.device_id] = False
