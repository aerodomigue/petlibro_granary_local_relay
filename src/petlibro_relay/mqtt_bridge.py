"""Bridge between the feeder's local broker and the PETLIBRO cloud.

The relay holds one connection to the local broker every feeder talks to
(once DNS for the cloud hostname is redirected to this host), and - through
`DeviceManager` - one authenticated cloud connection per device. This module
owns the local side and the routing between them; `DeviceContext` owns each
device's cloud side.

Routing is by topic. Every device topic names the device it belongs to, so a
message published locally is parsed, looked up in the manager, and handed to
that device's context. A message from an unknown or unenrolled device is
ignored rather than guessed at: forwarding it over some other device's
session would authenticate one feeder's traffic as another.

Neither side ever publishes directly from a network callback: messages are
handed off to a `MessageQueue` (durable, survives restarts) and a
`DeliveryPump` drains each direction independently. This means a stalled or
offline broker on one side never blocks traffic on the other, and whatever
arrived during an outage is replayed, in order, once the destination is
reachable again (subject to `replay_policy`, which drops commands that are
unsafe to act on late).

The two directions must never overlap in the topics they listen to, or the
bridge feeds itself: MQTT 3.1 has no "no local" subscription option, so a
client subscribed to a filter covering what it publishes receives its own
messages back from the broker. Subscribing locally to `<prefix>/#` while
republishing cloud commands onto `<prefix>/device/<cat>/sub` did exactly
that, and each lap also re-published upstream (where we subscribe to the
same `/sub` topics), amplifying without bound. The split is therefore
strict and directional:

    device -> cloud :  local subscribes to  dl/+/+/device/+/post
    cloud -> device :  upstream subscribes to <prefix>/device/<cat>/sub
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import paho.mqtt.client as mqtt
from paho.mqtt.client import Client, ConnectFlags, DisconnectFlags, MQTTMessage
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from . import protocol
from .config import RelayConfig
from .delivery_pump import DeliveryPump, PumpTarget
from .device_context import LOCAL_TO_UPSTREAM, UPSTREAM_TO_LOCAL
from .device_manager import DeviceManager
from .message_queue import MessageQueue
from .observability.telemetry import RelayTelemetry

if TYPE_CHECKING:
    from .sound_switch_control import SoundSwitchController

_LOGGER = logging.getLogger(__name__)

MQTT_PROTOCOL_VERSION = mqtt.MQTTv31
QOS_AT_MOST_ONCE = 0
PRIME_SUBSCRIBE_SETTLE_SECONDS = 1.0

# Client ID the relay uses on the local broker. Fixed, and always paired with
# clean_session=False, so the broker keeps this session (and its queued
# messages) across relay restarts.
LOCAL_CLIENT_ID = "relay-local"

# Device-agnostic device -> cloud filter. Matches any product/device, which is
# what makes a single local subscription serve every feeder, and still only
# ever covers "/post" (never the "/sub" topics we republish locally, which is
# what would make the bridge consume its own output).
ANY_DEVICE_POST_FILTER = "dl/+/+/device/+/post"


def prime_local_subscription(config: RelayConfig) -> None:
    """Register the local subscription before any device has connected.

    On a fresh install the relay can only bridge a device once
    `CredentialCaptureProxy` has learned its identity - which requires the
    feeder to connect first. The feeder then publishes its opening burst
    (`DEVICE_START_EVENT` and the NTP handshake) in the second or two before
    the bridge subscribes, and those messages are lost.

    Connecting briefly here as the same `clean_session=False` client the
    bridge will use, and subscribing to the device-agnostic "/post" filter,
    makes the broker hold matching messages for that session while it is
    offline (mosquitto's `queue_qos0_messages`). They are delivered as soon as
    the bridge connects for real, so nothing published during the gap is
    dropped.

    Failure here is not fatal: it only costs that first burst, so it is logged
    and execution continues.

    Args:
        config: Relay runtime configuration (local broker address).
    """
    client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=LOCAL_CLIENT_ID,
        protocol=MQTT_PROTOCOL_VERSION,
        clean_session=False,
    )
    try:
        client.connect(config.local_host, config.local_port, keepalive=config.keepalive_seconds)
        client.loop_start()
        client.subscribe(ANY_DEVICE_POST_FILTER, qos=QOS_AT_MOST_ONCE)
        # Give the SUBSCRIBE a moment to reach the broker before dropping the
        # connection, otherwise the session is stored without it.
        time.sleep(PRIME_SUBSCRIBE_SETTLE_SECONDS)
        client.loop_stop()
        client.disconnect()
    except OSError as error:
        _LOGGER.warning(
            "Could not pre-register the local subscription (%s); messages a feeder "
            "publishes before the bridge starts may be missed",
            error,
        )
        return
    _LOGGER.info(
        "Pre-registered local subscription %s for session %s", ANY_DEVICE_POST_FILTER, LOCAL_CLIENT_ID
    )


class MqttBridge:
    """Routes traffic between the local broker and each device's cloud session."""

    def __init__(
        self,
        config: RelayConfig,
        devices: DeviceManager,
        queue: MessageQueue,
        telemetry: RelayTelemetry,
    ) -> None:
        """Initialize the bridge, its local client, and both delivery pumps.

        Args:
            config: Relay runtime configuration.
            devices: Owner of every bridged device's context.
            queue: Durable queue backing both delivery directions.
            telemetry: Relay-wide telemetry for the local broker and events.
        """
        self._config = config
        self._devices = devices
        self._queue = queue
        self._telemetry = telemetry
        self._local_topic_filter = ANY_DEVICE_POST_FILTER
        self._unknown_devices_seen: set[str] = set()
        self._sound_switch_controller: SoundSwitchController | None = None

        self._local_client = self._build_local_client()
        self._local_to_upstream_pump = DeliveryPump(
            LOCAL_TO_UPSTREAM,
            queue,
            targets=self._upstream_targets,
            is_cloud_to_device=False,
            replay_rate_per_device=config.replay_rate_per_device,
            replay_rate_global=config.replay_rate_global,
            replay_start_delay_seconds=config.replay_start_delay_seconds,
            replay_jitter=config.replay_jitter,
        )
        self._upstream_to_local_pump = DeliveryPump(
            UPSTREAM_TO_LOCAL,
            queue,
            targets=self._local_targets,
            is_cloud_to_device=True,
        )

    def _build_local_client(self) -> Client:
        client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=LOCAL_CLIENT_ID,
            protocol=MQTT_PROTOCOL_VERSION,
            clean_session=False,
        )
        client.on_connect = self._on_local_connect
        client.on_message = self._on_local_message
        client.on_disconnect = self._on_local_disconnect
        return client

    # -- pump targets -------------------------------------------------------------

    def _upstream_targets(self) -> list[PumpTarget]:
        """Device -> cloud: each device publishes on its own cloud session.

        A device whose session is intentionally closed (because it is not
        locally present) has no target, so its backlog simply stays queued
        until it comes back rather than being dropped.
        """
        return [
            PumpTarget(
                context.device_id,
                client,
                context.telemetry,
                replay_ready=context.upstream_replay_ready,
            )
            for context in self._devices.list_devices()
            if (client := context.upstream_client) is not None
        ]

    def _local_targets(self) -> list[PumpTarget]:
        """Cloud -> device: every device is reached over the one local client."""
        return [
            PumpTarget(context.device_id, self._local_client, context.telemetry)
            for context in self._devices.list_devices()
        ]

    # -- lifecycle -----------------------------------------------------------------

    def run_forever(self) -> None:
        """Connect the local client and start the delivery pumps."""
        self._local_client.connect_async(
            self._config.local_host,
            self._config.local_port,
            keepalive=self._config.keepalive_seconds,
        )
        self._local_client.loop_start()
        self._local_to_upstream_pump.start()
        self._upstream_to_local_pump.start()

    def set_sound_switch_controller(self, controller: SoundSwitchController) -> None:
        """Attach the narrow UI control acknowledgement observer.

        This does not alter normal device-to-cloud handling: it only lets the
        controller correlate a matching service `/post` ACK before the same
        message continues into the existing queue and upstream bridge.
        """
        self._sound_switch_controller = controller

    def publish_sound_switch(self, device_id: str, product_id: str, payload: bytes) -> bool:
        """Publish an explicit interactive setting without durable replay.

        The caller owns the validated payload. This method constructs the
        fixed local `/service/sub` topic itself and immediately fails when the
        broker is unavailable; it never enqueues the command.
        """
        if not self._local_client.is_connected():
            return False
        topic = protocol.sub_topic(device_id, "service", product_id)
        result = self._local_client.publish(topic, payload, qos=QOS_AT_MOST_ONCE)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            return False
        try:
            result.wait_for_publish(timeout=1.0)
        except (RuntimeError, ValueError):
            return False
        return result.is_published()

    def stop(self) -> None:
        """Stop the delivery pumps and disconnect from the local broker.

        Device cloud sessions belong to `DeviceManager` and are stopped there,
        so this leaves them alone.
        """
        _LOGGER.info("Stopping bridge")
        self._local_to_upstream_pump.stop()
        self._upstream_to_local_pump.stop()
        self._local_client.loop_stop()
        self._local_client.disconnect()

    # -- local broker callbacks -------------------------------------------------

    def _on_local_connect(
        self,
        client: Client,
        userdata: object,
        connect_flags: ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        _LOGGER.info("Connected to local broker (reason=%s)", reason_code)
        self._telemetry.local_connected()
        # We connect with clean_session=False so the broker holds messages a
        # feeder published while the relay was down. The flip side is that the
        # broker also restores our *old* subscriptions: an earlier version of
        # this relay subscribed to "<prefix>/#", which matches the "/sub"
        # topics we republish locally and fed the bridge its own output. Those
        # subscriptions survive in the persisted session even after this code
        # stopped asking for them, so retire them explicitly on every connect.
        for context in self._devices.list_devices():
            client.unsubscribe(f"{context.topic_prefix}/#")
        client.subscribe(self._local_topic_filter, qos=QOS_AT_MOST_ONCE)
        _LOGGER.info("Subscribed to local topic filter: %s", self._local_topic_filter)

    def _on_local_disconnect(
        self,
        client: Client,
        userdata: object,
        disconnect_flags: DisconnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        _LOGGER.warning("Disconnected from local broker (reason=%s)", reason_code)
        self._telemetry.local_disconnected()

    def _on_local_message(self, client: Client, userdata: object, message: MQTTMessage) -> None:
        address = protocol.parse_topic(message.topic)
        if address is None or not address.is_post:
            _LOGGER.debug("Ignoring local message on non-device topic %s", message.topic)
            return

        context = self._devices.get_by_device_id(address.device_id)
        if context is None:
            self._warn_unknown_device_once(address.device_id, message.topic)
            return

        if self._sound_switch_controller is not None:
            self._sound_switch_controller.observe_device_message(
                address.device_id, message.topic, message.payload
            )

        _LOGGER.debug(
            "local -> queue(%s) for %s: %s (%d bytes)",
            LOCAL_TO_UPSTREAM,
            address.device_id,
            message.topic,
            len(message.payload),
        )
        action = context.handle_local_message(message.topic, message.payload, message.qos)
        if action is not None and action.response_topic is not None and action.response_payload is not None:
            client.publish(action.response_topic, action.response_payload, qos=QOS_AT_MOST_ONCE)

    def _warn_unknown_device_once(self, device_id: str, topic: str) -> None:
        """Log an unbridged device once, rather than on every message it sends."""
        if device_id in self._unknown_devices_seen:
            return
        self._unknown_devices_seen.add(device_id)
        _LOGGER.warning(
            "Ignoring %s: device %s is not bridged by this relay. It is either awaiting "
            "enrollment (PETLIBRO_AUTO_ENROLL is off), disabled, or has not completed a "
            "CONNECT through the capture proxy yet.",
            topic,
            device_id,
        )

    def forget_unknown_device(self, device_id: str) -> None:
        """Allow the unknown-device warning to fire again once it is enrolled."""
        self._unknown_devices_seen.discard(device_id)
