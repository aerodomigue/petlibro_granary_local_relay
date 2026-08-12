"""Background delivery pump that drains a MessageQueue direction onto an MQTT client."""

from __future__ import annotations

import logging
import threading

import paho.mqtt.client as mqtt

from .message_queue import MessageQueue

_LOGGER = logging.getLogger(__name__)

DISCONNECTED_POLL_INTERVAL_SECONDS = 2.0
EMPTY_QUEUE_POLL_INTERVAL_SECONDS = 1.0
PUBLISH_RETRY_INTERVAL_SECONDS = 2.0
STOP_JOIN_TIMEOUT_SECONDS = 5.0


class DeliveryPump:
    """Drains one direction of a `MessageQueue` onto its destination MQTT client.

    Runs in its own thread so a slow or disconnected destination never blocks
    message ingestion on the other side of the bridge. When the destination
    reconnects after an outage, the pump resumes draining automatically and
    logs how many backlogged messages it is replaying (the "resync").
    """

    def __init__(self, direction: str, queue: MessageQueue, destination_client: mqtt.Client) -> None:
        """Initialize the pump.

        Args:
            direction: Logical queue name this pump drains (must match the
                `direction` used when messages were enqueued).
            queue: Shared durable queue.
            destination_client: MQTT client to publish drained messages onto.
        """
        self._direction = direction
        self._queue = queue
        self._destination_client = destination_client
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"pump-{direction}", daemon=True)
        self._had_backlog_during_outage = False

    def start(self) -> None:
        """Start the background delivery thread."""
        self._thread.start()

    def stop(self) -> None:
        """Signal the pump to stop and wait for its thread to exit."""
        self._stop_event.set()
        self._thread.join(timeout=STOP_JOIN_TIMEOUT_SECONDS)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if not self._destination_client.is_connected():
                if self._queue.count(self._direction) > 0:
                    self._had_backlog_during_outage = True
                self._stop_event.wait(DISCONNECTED_POLL_INTERVAL_SECONDS)
                continue

            self._log_resync_if_needed()

            message = self._queue.peek_oldest(self._direction)
            if message is None:
                self._stop_event.wait(EMPTY_QUEUE_POLL_INTERVAL_SECONDS)
                continue

            result = self._destination_client.publish(message.topic, message.payload, qos=message.qos)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self._queue.remove(message.id)
            else:
                _LOGGER.warning(
                    "Publish failed for %s (topic=%s, rc=%s), will retry",
                    self._direction,
                    message.topic,
                    result.rc,
                )
                self._stop_event.wait(PUBLISH_RETRY_INTERVAL_SECONDS)

    def _log_resync_if_needed(self) -> None:
        if not self._had_backlog_during_outage:
            return
        pending = self._queue.count(self._direction)
        if pending:
            _LOGGER.info(
                "Destination for %s is back online, replaying %d backlogged message(s)",
                self._direction,
                pending,
            )
        self._had_backlog_during_outage = False
