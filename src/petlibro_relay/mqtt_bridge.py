"""Bidirectional MQTT bridge between the feeder's local broker and the PETLIBRO cloud.

Two independent MQTT connections are maintained:

* `local_client` connects to the local broker the feeder itself talks to
  (once DNS for the cloud hostname is redirected to this host).
* `upstream_client` connects to the real PETLIBRO cloud broker, authenticated
  as the device itself, using the credentials extracted from the device's own
  CONNECT packet.

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

    device -> cloud :  local subscribes to  <prefix>/device/+/post
    cloud -> device :  upstream subscribes to <prefix>/device/<cat>/sub
"""

from __future__ import annotations

import logging
from typing import List, Sequence

import paho.mqtt.client as mqtt
from paho.mqtt.client import Client, ConnectFlags, DisconnectFlags, MQTTMessage
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from .config import RelayConfig
from .delivery_pump import DeliveryPump
from .device_registry import DeviceIdentity
from .message_queue import MessageQueue
from .replay_policy import coalesce_key_for, extract_command, policy_for
from .state_cache import StateCache

_LOGGER = logging.getLogger(__name__)

MQTT_PROTOCOL_VERSION = mqtt.MQTTv31
QOS_AT_MOST_ONCE = 0
MIN_RECONNECT_DELAY_SECONDS = 1
MAX_RECONNECT_DELAY_SECONDS = 60

LOCAL_TO_UPSTREAM = "local-to-upstream"
UPSTREAM_TO_LOCAL = "upstream-to-local"

# Server -> device message categories, per the dl/<product>/<client_id>/device/<category>/sub
# topic pattern reverse-engineered from the device's own MQTT traffic.
UPSTREAM_SUBSCRIBE_CATEGORIES: Sequence[str] = (
    "service",
    "event",
    "ota",
    "ntp",
    "broadcast",
    "heart",
    "config",
    "system",
)


class MqttBridge:
    """Relays MQTT traffic between the feeder's local broker and the PETLIBRO cloud."""

    def __init__(
        self, config: RelayConfig, identity: DeviceIdentity, state_cache: StateCache, queue: MessageQueue
    ) -> None:
        """Initialize the bridge, its two MQTT clients, and their delivery pumps.

        Args:
            config: Relay runtime configuration.
            identity: The device's MQTT client ID, username and password -
                either manually configured or learned by
                `CredentialCaptureProxy` from the feeder's own CONNECT packet.
            state_cache: Cache used to persist the last payload per topic.
            queue: Durable queue backing both delivery directions.
        """
        self._config = config
        self._identity = identity
        self._state_cache = state_cache
        self._queue = queue
        self._topic_prefix = config.topic_prefix_override or f"dl/PLAF203/{identity.client_id}"
        # Device -> cloud only. Never a wildcard covering the "/sub" topics we
        # republish onto locally, or the bridge would consume its own output.
        self._local_topic_filter = f"{self._topic_prefix}/device/+/post"
        self._legacy_wildcard_filter = f"{self._topic_prefix}/#"
        self._pending_upstream_subscriptions: dict[int, str] = {}

        self._local_client = self._build_client("relay-local")
        self._local_client.on_connect = self._on_local_connect
        self._local_client.on_message = self._on_local_message
        self._local_client.on_disconnect = self._on_local_disconnect

        self._upstream_client = self._build_client(
            identity.client_id,
            username=identity.username,
            password=identity.password,
        )
        self._upstream_client.on_connect = self._on_upstream_connect
        self._upstream_client.on_connect_fail = self._on_upstream_connect_fail
        self._upstream_client.on_subscribe = self._on_upstream_subscribe
        self._upstream_client.on_message = self._on_upstream_message
        self._upstream_client.on_disconnect = self._on_upstream_disconnect

        self._local_to_upstream_pump = DeliveryPump(
            LOCAL_TO_UPSTREAM, queue, self._upstream_client, is_cloud_to_device=False
        )
        self._upstream_to_local_pump = DeliveryPump(
            UPSTREAM_TO_LOCAL, queue, self._local_client, is_cloud_to_device=True
        )

    def _build_client(
        self, client_id: str, username: str | None = None, password: str | None = None
    ) -> Client:
        client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=MQTT_PROTOCOL_VERSION,
            clean_session=False,
        )
        if username is not None:
            client.username_pw_set(username, password)
        client.reconnect_delay_set(min_delay=MIN_RECONNECT_DELAY_SECONDS, max_delay=MAX_RECONNECT_DELAY_SECONDS)
        return client

    def run_forever(self) -> None:
        """Connect both clients, start the delivery pumps, and block until `stop()`."""
        self._local_client.connect_async(
            self._config.local_host, self._config.local_port, keepalive=self._config.keepalive_seconds
        )
        self._upstream_client.connect_async(
            self._config.upstream_host, self._config.upstream_port, keepalive=self._config.keepalive_seconds
        )

        self._local_client.loop_start()
        self._upstream_client.loop_start()
        self._local_to_upstream_pump.start()
        self._upstream_to_local_pump.start()

    def stop(self) -> None:
        """Stop the delivery pumps and disconnect both MQTT clients."""
        _LOGGER.info("Stopping relay")
        self._local_to_upstream_pump.stop()
        self._upstream_to_local_pump.stop()

        self._local_client.loop_stop()
        self._local_client.disconnect()
        self._upstream_client.loop_stop()
        self._upstream_client.disconnect()

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
        # We connect with clean_session=False so the broker holds messages the
        # feeder published while the relay was down. The flip side is that the
        # broker also restores our *old* subscriptions: an earlier version of
        # this relay subscribed to "<prefix>/#", which matches the "/sub"
        # topics we republish locally and fed the bridge its own output. That
        # subscription survives in the persisted session even after this code
        # stopped asking for it, so retire it explicitly on every connect.
        client.unsubscribe(self._legacy_wildcard_filter)
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

    def _on_local_message(self, client: Client, userdata: object, message: MQTTMessage) -> None:
        _LOGGER.debug("local -> queue(%s): %s (%d bytes)", LOCAL_TO_UPSTREAM, message.topic, len(message.payload))
        self._state_cache.update(message.topic, message.payload)
        self._enqueue(LOCAL_TO_UPSTREAM, message, is_cloud_to_device=False)

    # -- upstream (cloud) callbacks ----------------------------------------------

    def _on_upstream_connect(
        self,
        client: Client,
        userdata: object,
        connect_flags: ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        _LOGGER.info("Connected to upstream PETLIBRO broker (reason=%s)", reason_code)
        # Only the explicit "/sub" (cloud -> device) topics. Never a wildcard:
        # besides being denied by the cloud ACL, "<prefix>/#" would also match
        # the "/post" topics this same client publishes upstream, so the
        # broker would echo them straight back into the relay.
        for category in UPSTREAM_SUBSCRIBE_CATEGORIES:
            topic = f"{self._topic_prefix}/device/{category}/sub"
            result, mid = client.subscribe(topic, qos=QOS_AT_MOST_ONCE)
            if result == mqtt.MQTT_ERR_SUCCESS and mid is not None:
                self._pending_upstream_subscriptions[mid] = topic

    def _on_upstream_connect_fail(self, client: Client, userdata: object) -> None:
        # Fires on TCP/DNS-level failures (refused, unreachable, name resolution).
        # A TCP connect that succeeds but never receives a CONNACK (observed with
        # some of mqtt.us.petlibro.com's DNS-round-robin IPs) surfaces instead as
        # an on_disconnect warning once the keepalive timeout gives up on it.
        _LOGGER.warning("Failed to establish TCP connection to upstream PETLIBRO broker, retrying")

    def _on_upstream_subscribe(
        self,
        client: Client,
        userdata: object,
        mid: int,
        reason_code_list: List[ReasonCode],
        properties: Properties | None,
    ) -> None:
        topic = self._pending_upstream_subscriptions.pop(mid, "<unknown>")
        for reason_code in reason_code_list:
            _LOGGER.info(
                "Upstream subscription %s -> %s (code=%s)",
                topic,
                "denied" if reason_code.is_failure else "granted",
                reason_code,
            )

    def _on_upstream_disconnect(
        self,
        client: Client,
        userdata: object,
        disconnect_flags: DisconnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        _LOGGER.warning("Disconnected from upstream PETLIBRO broker (reason=%s)", reason_code)

    def _on_upstream_message(self, client: Client, userdata: object, message: MQTTMessage) -> None:
        _LOGGER.debug(
            "upstream -> queue(%s): %s (%d bytes)", UPSTREAM_TO_LOCAL, message.topic, len(message.payload)
        )
        self._state_cache.update(message.topic, message.payload)
        self._enqueue(UPSTREAM_TO_LOCAL, message, is_cloud_to_device=True)

    # -- shared ------------------------------------------------------------------

    def _enqueue(self, direction: str, message: MQTTMessage, is_cloud_to_device: bool) -> None:
        """Queue a message for delivery, letting state-carrying commands supersede older ones."""
        command = extract_command(message.payload)
        policy = policy_for(is_cloud_to_device, command)
        coalesce_key = coalesce_key_for(message.topic, command, policy)
        self._queue.enqueue(direction, message.topic, message.payload, message.qos, coalesce_key)
