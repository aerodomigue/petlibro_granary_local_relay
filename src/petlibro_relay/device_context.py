"""Everything the relay owns on behalf of one device.

A `DeviceContext` is the unit of isolation. It holds one device's identity,
its own MQTT session to the PETLIBRO cloud, its own upstream state machine,
its own responder and its own view of the shared durable stores - which are
themselves keyed by device id.

Nothing here is shared between devices. That is the whole point: two feeders
have unrelated credentials, unrelated cloud outages and unrelated backlogs, so
one going dark must be invisible to the other.

The upstream session deliberately stays one-per-device rather than multiplexed:
the cloud authenticates the MQTT connection *as the device*, so a shared
session could only ever speak for one of them.
"""

from __future__ import annotations

import logging
from typing import List, Sequence

import paho.mqtt.client as mqtt
from paho.mqtt.client import Client, ConnectFlags, DisconnectFlags, MQTTMessage
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from . import protocol
from .config import RelayConfig
from .device_registry import DeviceIdentity
from .local_responder import Decision, LocalResponder, ResponderAction, UpstreamState
from .message_queue import MessageQueue
from .observability.telemetry import DeviceTelemetry, UpstreamTransition, UpstreamTransitionKind
from .replay_policy import coalesce_key_for, extract_command, policy_for
from .state_cache import StateCache
from .state_shadow import StateShadow

_LOGGER = logging.getLogger(__name__)

MQTT_PROTOCOL_VERSION = mqtt.MQTTv31
QOS_AT_MOST_ONCE = 0
MIN_RECONNECT_DELAY_SECONDS = 1
MAX_RECONNECT_DELAY_SECONDS = 60

LOCAL_TO_UPSTREAM = "local-to-upstream"
UPSTREAM_TO_LOCAL = "upstream-to-local"

# Server -> device message categories, per the dl/<product>/<device>/device/<category>/sub
# topic pattern reverse-engineered from the device's own MQTT traffic. Never a
# wildcard: besides being denied by the cloud ACL, "<prefix>/#" would also match
# the "/post" topics this same client publishes upstream, so the broker would
# echo them straight back into the relay.
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


class DeviceContext:
    """One device's identity, upstream session, state and metrics."""

    def __init__(
        self,
        identity: DeviceIdentity,
        config: RelayConfig,
        queue: MessageQueue,
        shadow: StateShadow,
        state_cache: StateCache,
        telemetry: DeviceTelemetry,
        responder: LocalResponder | None,
    ) -> None:
        """Build a device's context without connecting anything yet.

        Args:
            identity: This device's MQTT credentials and product.
            config: Relay runtime configuration.
            queue: Shared durable queue; every access is scoped to this
                device's id.
            shadow: Shared state shadow; likewise scoped by device id.
            state_cache: Shared last-payload-per-topic cache. Topics already
                carry the device id, so entries never collide.
            telemetry: This device's own metrics.
            responder: This device's own local responder, or `None` to stay a
                pure pipe.
        """
        self._identity = identity
        self._config = config
        self._queue = queue
        self._shadow = shadow
        self._state_cache = state_cache
        self._telemetry = telemetry
        self._responder = responder
        self._topic_prefix = protocol.topic_prefix(identity.client_id, identity.product_id)
        self._pending_upstream_subscriptions: dict[int, str] = {}
        self._stopping = False

        self._upstream_client = self._build_upstream_client()

    # -- identity ----------------------------------------------------------------

    @property
    def device_id(self) -> str:
        """Return this device's id, which is also its MQTT client id."""
        return self._identity.client_id

    @property
    def product_id(self) -> str:
        """Return the product this device reports itself as."""
        return self._identity.product_id

    @property
    def topic_prefix(self) -> str:
        """Return this device's `dl/<product>/<device>` topic prefix."""
        return self._topic_prefix

    @property
    def upstream_client(self) -> Client:
        """Return the Paho client bound to this device's cloud session."""
        return self._upstream_client

    @property
    def upstream_state(self) -> UpstreamState:
        """Return how far along this device's cloud session actually is."""
        return self._telemetry.upstream_state

    @property
    def telemetry(self) -> DeviceTelemetry:
        """Return this device's metrics."""
        return self._telemetry

    @property
    def responder(self) -> LocalResponder | None:
        """Return this device's local responder, if fallback is configured."""
        return self._responder

    # -- lifecycle ---------------------------------------------------------------

    def _build_upstream_client(self) -> Client:
        client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=self._identity.client_id,
            protocol=MQTT_PROTOCOL_VERSION,
            clean_session=False,
        )
        client.username_pw_set(self._identity.username, self._identity.password)
        client.reconnect_delay_set(
            min_delay=MIN_RECONNECT_DELAY_SECONDS, max_delay=MAX_RECONNECT_DELAY_SECONDS
        )
        client.on_connect = self._on_upstream_connect
        client.on_connect_fail = self._on_upstream_connect_fail
        client.on_subscribe = self._on_upstream_subscribe
        client.on_message = self._on_upstream_message
        client.on_disconnect = self._on_upstream_disconnect
        return client

    def start(self) -> None:
        """Open this device's cloud session in its own network thread."""
        self._log_upstream_transition(self._telemetry.upstream_connect_attempt())
        self._upstream_client.connect_async(
            self._config.upstream_host,
            self._config.upstream_port,
            keepalive=self._config.keepalive_seconds,
        )
        self._upstream_client.loop_start()
        _LOGGER.info(
            "Started upstream session for %s (product=%s)", self.device_id, self.product_id
        )

    def stop(self) -> None:
        """Close this device's cloud session without disturbing any other."""
        self._stopping = True
        self._upstream_client.loop_stop()
        self._upstream_client.disconnect()
        _LOGGER.info("Stopped upstream session for %s", self.device_id)

    # -- device -> cloud ---------------------------------------------------------

    def handle_local_message(self, topic: str, payload: bytes, qos: int) -> ResponderAction | None:
        """Process one message this device published on the local broker.

        Returns:
            The responder action when a local answer must be published back to
            the device, otherwise `None`. Publishing is left to the caller,
            which owns the local client.
        """
        self._state_cache.update(topic, payload)
        self._observe_ntp(payload, source="device")

        if self._responder is not None:
            action = self._responder.decide(
                self.device_id, topic, payload, self.upstream_state
            )
            if action.decision is Decision.RESPOND_LOCAL:
                # Answered from local knowledge, so this occurrence is not sent
                # upstream: the cloud must not later answer the same question a
                # second time (see README, "Local responder").
                self._telemetry.increment("local_responses")
                return action
            if action.decision is Decision.IGNORE:
                return None

        self._enqueue(LOCAL_TO_UPSTREAM, topic, payload, qos, is_cloud_to_device=False)
        return None

    # -- cloud -> device ---------------------------------------------------------

    def _on_upstream_message(self, client: Client, userdata: object, message: MQTTMessage) -> None:
        _LOGGER.debug(
            "upstream -> queue(%s) for %s: %s (%d bytes)",
            UPSTREAM_TO_LOCAL,
            self.device_id,
            message.topic,
            len(message.payload),
        )
        self._state_cache.update(message.topic, message.payload)
        self._observe_ntp(message.payload, source="cloud")

        if self._responder is not None:
            # Learn from the cloud first: this is where last-known-good config
            # and feeding plans come from.
            self._responder.observe_cloud_message(self.device_id, message.topic, message.payload)
            if self._responder.is_suppressed_cloud_response(self.device_id, message.payload):
                self._telemetry.increment("suppressed_late_cloud_responses")
                return

        self._enqueue(
            UPSTREAM_TO_LOCAL,
            message.topic,
            message.payload,
            message.qos,
            is_cloud_to_device=True,
        )

    # -- upstream callbacks ------------------------------------------------------

    def _on_upstream_connect(
        self,
        client: Client,
        userdata: object,
        connect_flags: ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        # Only a CONNACK makes the cloud "online". A completed TCP handshake
        # does not: PETLIBRO has been observed accepting the socket, ignoring
        # the CONNECT for ~30s, then resetting.
        if reason_code.is_failure:
            self._log_upstream_transition(self._telemetry.upstream_refused(str(reason_code)))
            return
        self._log_upstream_transition(self._telemetry.upstream_online())
        for category in UPSTREAM_SUBSCRIBE_CATEGORIES:
            topic = f"{self._topic_prefix}/{protocol.DEVICE_SEGMENT}/{category}/{protocol.SUB_SUFFIX}"
            result, mid = client.subscribe(topic, qos=QOS_AT_MOST_ONCE)
            if result == mqtt.MQTT_ERR_SUCCESS and mid is not None:
                self._pending_upstream_subscriptions[mid] = topic

    def _on_upstream_connect_fail(self, client: Client, userdata: object) -> None:
        # Fires on TCP/DNS-level failures (refused, unreachable, name resolution).
        # A TCP connect that succeeds but never receives a CONNACK (observed with
        # some of mqtt.us.petlibro.com's DNS-round-robin IPs) surfaces instead as
        # an on_disconnect warning once the keepalive timeout gives up on it.
        self._log_upstream_transition(self._telemetry.upstream_connect_failed())

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
        if self._stopping:
            _LOGGER.debug(
                "Upstream MQTT stopped intentionally for %s (reason=%s)", self.device_id, reason_code
            )
            return
        self._log_upstream_transition(
            self._telemetry.upstream_disconnected(
                str(reason_code), disconnect_flags=str(disconnect_flags)
            )
        )
        self._log_upstream_transition(self._telemetry.upstream_connect_attempt())

    # -- shared ------------------------------------------------------------------

    def _enqueue(
        self, direction: str, topic: str, payload: bytes, qos: int, is_cloud_to_device: bool
    ) -> None:
        """Queue a message for this device, letting state-carrying commands supersede older ones."""
        command = extract_command(payload)
        policy = policy_for(is_cloud_to_device, command)
        coalesce_key = coalesce_key_for(topic, command, policy)
        self._queue.enqueue(
            self.device_id,
            direction,
            topic,
            payload,
            qos,
            coalesce_key,
            product_id=self.product_id,
        )

    def _observe_ntp(self, payload: bytes, source: str) -> None:
        """Record NTP session-establishment traffic without changing its flow."""
        command = extract_command(payload)
        if command == protocol.Command.NTP and source == "device":
            self._telemetry.increment("ntp_requests")
        elif command == protocol.Command.NTP_SYNC:
            self._telemetry.increment(f"ntp_sync_from_{source}")

    def _log_upstream_transition(self, transition: UpstreamTransition) -> None:
        """Emit semantically accurate upstream logs from telemetry decisions."""
        device_id = transition.device_id
        if transition.kind is UpstreamTransitionKind.CONNECT_ATTEMPT:
            _LOGGER.debug(
                "Upstream MQTT CONNECT device=%s attempt=%d state_before=%s state_after=%s",
                device_id,
                transition.attempt,
                transition.state_before,
                transition.state_after,
            )
            return
        if transition.kind is UpstreamTransitionKind.SESSION_LOST:
            _LOGGER.warning(
                "UPSTREAM lost device=%s reason=%s session_duration=%.1fs state_before=%s "
                "disconnect_flags=%s",
                device_id,
                transition.reason,
                transition.session_duration_seconds or 0.0,
                transition.state_before,
                transition.disconnect_flags,
            )
            return
        if transition.kind is UpstreamTransitionKind.ONLINE:
            _LOGGER.info(
                "UPSTREAM online device=%s downtime=%.1fs state_before=%s",
                device_id,
                transition.downtime_seconds or 0.0,
                transition.state_before,
            )
            return
        if transition.kind is UpstreamTransitionKind.RESTORED:
            _LOGGER.info(
                "UPSTREAM restored device=%s downtime=%.1fs failed_attempts=%d state_before=%s",
                device_id,
                transition.downtime_seconds or 0.0,
                transition.failed_attempts,
                transition.state_before,
            )
            return
        if transition.kind is UpstreamTransitionKind.CONNACK_REFUSED:
            _LOGGER.warning(
                "Upstream CONNACK refused device=%s reason_code=%s attempt=%d state_before=%s",
                device_id,
                transition.reason_code,
                transition.attempt,
                transition.state_before,
            )
        else:
            _LOGGER.debug(
                "Upstream reconnect failed device=%s attempt=%d reason=%s state_before=%s",
                device_id,
                transition.attempt,
                transition.reason,
                transition.state_before,
            )
        if transition.offline_summary_due:
            _LOGGER.warning(
                "UPSTREAM still offline device=%s downtime=%.1fs attempts=%d last_reason=%s",
                device_id,
                transition.downtime_seconds or 0.0,
                transition.attempt,
                transition.reason,
            )
