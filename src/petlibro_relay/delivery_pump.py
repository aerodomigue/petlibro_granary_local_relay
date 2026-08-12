"""Background delivery pump that drains a MessageQueue direction onto an MQTT client."""

from __future__ import annotations

import logging
import threading
import time

import paho.mqtt.client as mqtt

from .message_queue import MessageQueue
from .observability.telemetry import RelayTelemetry
from .replay_policy import extract_command, policy_for

_LOGGER = logging.getLogger(__name__)

DISCONNECTED_POLL_INTERVAL_SECONDS = 2.0
EMPTY_QUEUE_POLL_INTERVAL_SECONDS = 1.0
PUBLISH_RETRY_INTERVAL_SECONDS = 2.0
PUBLISH_CONFIRM_TIMEOUT_SECONDS = 10.0
STOP_JOIN_TIMEOUT_SECONDS = 5.0


class DeliveryPump:
    """Drains one direction of a `MessageQueue` onto its destination MQTT client.

    Runs in its own thread so a slow or disconnected destination never blocks
    message ingestion on the other side of the bridge. When the destination
    reconnects after an outage, the pump resumes draining automatically and
    logs how many backlogged messages it is replaying (the "resync").

    Messages whose replay policy has expired are dropped rather than
    delivered late - see `replay_policy` for why that matters in the
    cloud -> device direction.
    """

    def __init__(
        self,
        direction: str,
        queue: MessageQueue,
        destination_client: mqtt.Client,
        is_cloud_to_device: bool,
        telemetry: RelayTelemetry | None = None,
    ) -> None:
        """Initialize the pump.

        Args:
            direction: Logical queue name this pump drains (must match the
                `direction` used when messages were enqueued).
            queue: Shared durable queue.
            destination_client: MQTT client to publish drained messages onto.
            is_cloud_to_device: Whether this direction carries commands that
                act on the physical world when delivered, and therefore need
                replay policies applied.
            telemetry: Optional runtime-only counters for the dashboard.
        """
        self._direction = direction
        self._queue = queue
        self._destination_client = destination_client
        self._is_cloud_to_device = is_cloud_to_device
        self._telemetry = telemetry
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

            if self._is_expired(message.payload, message.created_at, message.topic):
                self._queue.remove(message.id)
                if self._telemetry is not None:
                    self._telemetry.increment("queue_expired")
                continue

            if self._publish_confirmed(message.topic, message.payload, message.qos):
                self._queue.remove(message.id)
                if self._telemetry is not None:
                    self._telemetry.increment(f"queue_delivered_{self._direction}")
            else:
                self._stop_event.wait(PUBLISH_RETRY_INTERVAL_SECONDS)

    def _is_expired(self, payload: bytes, created_at: float, topic: str) -> bool:
        """Return True if this message's replay policy says it's too stale to deliver."""
        command = extract_command(payload)
        policy = policy_for(self._is_cloud_to_device, command)
        if policy.max_age_seconds is None:
            return False
        age_seconds = time.time() - created_at
        if age_seconds <= policy.max_age_seconds:
            return False
        _LOGGER.warning(
            "Dropping stale %s message on %s (cmd=%s, age=%.1fs > %.1fs) rather than acting on it late",
            self._direction,
            topic,
            command,
            age_seconds,
            policy.max_age_seconds,
        )
        return True

    def _publish_confirmed(self, topic: str, payload: bytes, qos: int) -> bool:
        """Publish and wait for the client to confirm the packet actually went out.

        `publish()` returning MQTT_ERR_SUCCESS only means paho accepted the
        message into its outgoing buffer; the socket write can still fail.
        Waiting for publication before removing the message from the durable
        queue is what makes a crash mid-send replay rather than lose it.
        """
        message_info = self._destination_client.publish(topic, payload, qos=qos)
        if message_info.rc != mqtt.MQTT_ERR_SUCCESS:
            _LOGGER.warning(
                "Publish rejected for %s (topic=%s, rc=%s), will retry",
                self._direction,
                topic,
                message_info.rc,
            )
            return False
        try:
            message_info.wait_for_publish(timeout=PUBLISH_CONFIRM_TIMEOUT_SECONDS)
        except (ValueError, RuntimeError) as error:
            _LOGGER.warning(
                "Publish not confirmed for %s (topic=%s): %s, will retry", self._direction, topic, error
            )
            return False
        if not message_info.is_published():
            _LOGGER.warning(
                "Publish timed out unconfirmed for %s (topic=%s), will retry", self._direction, topic
            )
            return False
        return True

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
            if self._telemetry is not None:
                self._telemetry.increment(f"queue_replayed_{self._direction}", pending)
        self._had_backlog_during_outage = False
