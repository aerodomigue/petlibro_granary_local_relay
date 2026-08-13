#!/usr/bin/env python3
"""Run one reversible, local-only motion-detection synchronization probe.

This is deliberately not a general MQTT publisher. It connects only to the
internal ``mosquitto`` service, accepts only the known PLAF203 target, and can
emit only ``ATTR_SET_SERVICE`` with one boolean field:
``motionDetectionSwitch``. The original value is required and is restored in
``finally`` after a bounded observation interval.

Run this tool from inside the relay Docker network, for example by copying it
to the running relay container. It has no PETLIBRO credentials and cannot
contact the PETLIBRO broker.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Final

import paho.mqtt.client as mqtt
from paho.mqtt.client import Client, ConnectFlags, DisconnectFlags, MQTTMessage
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from petlibro_relay import protocol
from petlibro_relay.observability.sanitizer import sanitize_value

_LOGGER = logging.getLogger(__name__)

TARGET_DEVICE_ID: Final = "AF03040302A2B5B2CD60"
TARGET_PRODUCT_ID: Final = "PLAF203"
LOCAL_BROKER_HOST: Final = "mosquitto"
LOCAL_BROKER_PORT: Final = 1883
MQTT_PROTOCOL_VERSION: Final = mqtt.MQTTv31
QOS_AT_MOST_ONCE: Final = 0
CONNECT_TIMEOUT_SECONDS: Final = 10.0
PUBLISH_TIMEOUT_SECONDS: Final = 10.0
POLL_INTERVAL_SECONDS: Final = 0.25
MIN_OBSERVE_SECONDS: Final = 15
MAX_TEST_OBSERVE_SECONDS: Final = 300
MAX_RESTORE_OBSERVE_SECONDS: Final = 120
CLIENT_ID_PREFIX: Final = "motion-probe-"
MQTT_31_CLIENT_ID_LIMIT: Final = 23


@dataclass(frozen=True, slots=True)
class Observation:
    """One local MQTT observation, safe for the diagnostic report."""

    timestamp: float
    source: str
    topic: str
    payload: dict[str, Any]


class MotionDetectionProbe:
    """Publish exactly one reversible setting test and observe its local effects."""

    def __init__(
        self,
        original_value: bool,
        test_observe_seconds: int,
        restore_observe_seconds: int,
    ) -> None:
        """Initialize a bounded probe for the single approved setting.

        Args:
            original_value: Baseline value to restore after the probe.
            test_observe_seconds: Time to observe the test value before restoration.
            restore_observe_seconds: Time to verify the restoration.
        """
        self._original_value = original_value
        self._test_value = not original_value
        self._test_observe_seconds = test_observe_seconds
        self._restore_observe_seconds = restore_observe_seconds
        self._connected = threading.Event()
        self._subscribed = threading.Event()
        self._pending_subscriptions: set[int] = set()
        self._observations: list[Observation] = []
        self._lock = threading.Lock()
        self._test_msg_id: str | None = None
        self._restore_msg_id: str | None = None
        self._connection_error: str | None = None
        self._client = self._build_client()

    def run(self) -> int:
        """Run the test, always attempting local restoration after test publication."""
        self._client.connect_async(
            LOCAL_BROKER_HOST,
            LOCAL_BROKER_PORT,
            keepalive=30,
        )
        self._client.loop_start()
        published_test = False
        try:
            if not self._connected.wait(CONNECT_TIMEOUT_SECONDS):
                _LOGGER.error("MOTION_PROBE_ABORT local broker connection timed out")
                return 2
            if self._connection_error is not None:
                _LOGGER.error("MOTION_PROBE_ABORT local broker refused connection: %s", self._connection_error)
                return 2
            if not self._subscribed.wait(CONNECT_TIMEOUT_SECONDS):
                _LOGGER.error("MOTION_PROBE_ABORT target subscriptions were not acknowledged")
                return 2

            self._test_msg_id = self._publish_setting(self._test_value, phase="test")
            published_test = True
            self._observe_for(self._test_observe_seconds)
            return 0
        finally:
            if published_test:
                try:
                    self._restore_msg_id = self._publish_setting(self._original_value, phase="restore")
                    self._observe_for(self._restore_observe_seconds)
                except (OSError, RuntimeError, ValueError) as error:
                    _LOGGER.error("MOTION_PROBE_RESTORE_FAILED error=%s", error)
                    self._report(status="restore_failed")
                else:
                    self._report(status="restored")
            else:
                self._report(status="aborted_before_write")
            self._client.loop_stop()
            self._client.disconnect()

    def _build_client(self) -> Client:
        suffix_length = MQTT_31_CLIENT_ID_LIMIT - len(CLIENT_ID_PREFIX)
        client_id = f"{CLIENT_ID_PREFIX}{uuid.uuid4().hex[:suffix_length]}"
        client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=MQTT_PROTOCOL_VERSION,
            clean_session=True,
        )
        client.on_connect = self._on_connect
        client.on_subscribe = self._on_subscribe
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        return client

    def _on_connect(
        self,
        client: Client,
        userdata: object,
        connect_flags: ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if reason_code.is_failure:
            self._connection_error = str(reason_code)
            self._connected.set()
            return
        topics = (
            protocol.sub_topic(TARGET_DEVICE_ID, "service", TARGET_PRODUCT_ID),
            f"{protocol.topic_prefix(TARGET_DEVICE_ID, TARGET_PRODUCT_ID)}/device/service/post",
        )
        for topic in topics:
            result, mid = client.subscribe(topic, qos=QOS_AT_MOST_ONCE)
            if result != mqtt.MQTT_ERR_SUCCESS or mid is None:
                self._connection_error = f"subscribe rejected for {topic}: rc={result}"
                self._connected.set()
                return
            self._pending_subscriptions.add(mid)
        self._connected.set()

    def _on_subscribe(
        self,
        client: Client,
        userdata: object,
        mid: int,
        reason_codes: list[ReasonCode],
        properties: Properties | None,
    ) -> None:
        if any(reason_code.is_failure for reason_code in reason_codes):
            self._connection_error = f"subscription refused: {reason_codes}"
            self._subscribed.set()
            return
        self._pending_subscriptions.discard(mid)
        if not self._pending_subscriptions:
            self._subscribed.set()

    def _on_message(self, client: Client, userdata: object, message: MQTTMessage) -> None:
        try:
            decoded = json.loads(message.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            _LOGGER.warning("MOTION_PROBE_IGNORED non-JSON message topic=%s", message.topic)
            return
        if not isinstance(decoded, dict):
            _LOGGER.warning("MOTION_PROBE_IGNORED non-object JSON topic=%s", message.topic)
            return
        source = "device_post" if message.topic.endswith("/post") else "local_sub"
        observation = Observation(time.time(), source, message.topic, sanitize_value(decoded))
        with self._lock:
            self._observations.append(observation)
        _LOGGER.info(
            "MOTION_PROBE_OBSERVED source=%s topic=%s payload=%s",
            source,
            message.topic,
            json.dumps(observation.payload, sort_keys=True, separators=(",", ":")),
        )

    def _on_disconnect(
        self,
        client: Client,
        userdata: object,
        disconnect_flags: DisconnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if not reason_code.is_failure:
            return
        _LOGGER.warning("MOTION_PROBE local broker disconnected reason=%s", reason_code)

    def _publish_setting(self, value: bool, phase: str) -> str:
        """Publish the one allowlisted local ``ATTR_SET_SERVICE`` message.

        Args:
            value: Requested boolean motion-detection state.
            phase: Either ``test`` or ``restore`` for the audit log.

        Returns:
            The freshly created message id.

        Raises:
            RuntimeError: If Paho cannot confirm local publication.
        """
        if phase not in ("test", "restore"):
            raise ValueError(f"Unexpected probe phase: {phase}")
        message_id = uuid.uuid4().hex
        payload = build_motion_payload(value, message_id, int(time.time() * 1000))
        topic = protocol.sub_topic(TARGET_DEVICE_ID, "service", TARGET_PRODUCT_ID)
        encoded_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        _LOGGER.warning(
            "MOTION_PROBE_PUBLISH phase=%s device=%s topic=%s payload=%s",
            phase,
            TARGET_DEVICE_ID,
            topic,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )
        result = self._client.publish(topic, encoded_payload, qos=QOS_AT_MOST_ONCE)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"local publish rejected: rc={result.rc}")
        result.wait_for_publish(timeout=PUBLISH_TIMEOUT_SECONDS)
        if not result.is_published():
            raise RuntimeError("local publish was not confirmed")
        return message_id

    def _observe_for(self, duration_seconds: int) -> None:
        deadline = time.monotonic() + duration_seconds
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)

    def _report(self, status: str) -> None:
        with self._lock:
            observations = [asdict(observation) for observation in self._observations]
        _LOGGER.warning(
            "MOTION_PROBE_RESULT %s",
            json.dumps(
                {
                    "status": status,
                    "device_id": TARGET_DEVICE_ID,
                    "original_value": self._original_value,
                    "test_value": self._test_value,
                    "test_msg_id": self._test_msg_id,
                    "restore_msg_id": self._restore_msg_id,
                    "observations": observations,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )


def parse_bool(value: str) -> bool:
    """Parse one explicit JSON-style boolean command-line value."""
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def build_motion_payload(value: bool, message_id: str, timestamp_ms: int) -> dict[str, object]:
    """Build the exact, allowlisted ``ATTR_SET_SERVICE`` payload shape.

    Args:
        value: Requested motion-detection switch value.
        message_id: Fresh correlation identifier for this command.
        timestamp_ms: Current Unix timestamp in milliseconds.

    Returns:
        The only payload the diagnostic is permitted to publish.
    """
    return {
        "cmd": protocol.Command.ATTR_SET_SERVICE,
        "ts": timestamp_ms,
        "msgId": message_id,
        "motionDetectionSwitch": value,
    }


def positive_observation_seconds(value: str, maximum: int) -> int:
    """Parse a bounded observation duration."""
    try:
        seconds = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected an integer") from error
    if not MIN_OBSERVE_SECONDS <= seconds <= maximum:
        raise argparse.ArgumentTypeError(
            f"expected {MIN_OBSERVE_SECONDS}..{maximum} seconds"
        )
    return seconds


def build_parser() -> argparse.ArgumentParser:
    """Build the strict command line for the one approved diagnostic."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--original-value", required=True, type=parse_bool)
    parser.add_argument(
        "--test-observe-seconds",
        default=120,
        type=lambda value: positive_observation_seconds(value, MAX_TEST_OBSERVE_SECONDS),
    )
    parser.add_argument(
        "--restore-observe-seconds",
        default=45,
        type=lambda value: positive_observation_seconds(value, MAX_RESTORE_OBSERVE_SECONDS),
    )
    return parser


def main(arguments: list[str] | None = None) -> int:
    """Validate the fixed target then run the probe.

    Args:
        arguments: Optional testable command-line arguments.

    Returns:
        Process status code.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parsed = build_parser().parse_args(arguments)
    if parsed.device_id != TARGET_DEVICE_ID:
        _LOGGER.error("MOTION_PROBE_ABORT unexpected device id")
        return 2
    probe = MotionDetectionProbe(
        parsed.original_value,
        parsed.test_observe_seconds,
        parsed.restore_observe_seconds,
    )
    return probe.run()


if __name__ == "__main__":
    sys.exit(main())
