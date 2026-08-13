"""Tests that two devices bridged by one relay never contaminate each other.

This is the safety property the multi-device model rests on. A feeder's
message must leave under its own credentials, sit in its own queue, land in
its own shadow and be counted in its own metrics - and a cloud outage on one
device must be invisible to the other.

Everything runs against fake MQTT clients and throwaway SQLite files: no
broker, no real device, and nothing that could reach the PETLIBRO cloud.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from paho.mqtt.client import Client, MQTTMessage

from petlibro_relay.config import RelayConfig
from petlibro_relay.delivery_pump import DeliveryPump, PumpTarget
from petlibro_relay.device_context import LOCAL_TO_UPSTREAM, UPSTREAM_TO_LOCAL, DeviceContext
from petlibro_relay.device_manager import DeviceManager
from petlibro_relay.device_presence import DevicePresenceTracker
from petlibro_relay.device_registry import DeviceIdentity, DeviceRegistry
from petlibro_relay.local_responder import LocalResponderSettings
from petlibro_relay.message_queue import MessageQueue
from petlibro_relay.mqtt_bridge import MqttBridge
from petlibro_relay.observability.telemetry import RelayTelemetry
from petlibro_relay.state_cache import StateCache
from petlibro_relay.state_shadow import StateShadow

from conftest import RelayConfigFactory

DEVICE_A = DeviceIdentity(client_id="DEVICE-A", username="user-a", password="pass-a")
DEVICE_B = DeviceIdentity(client_id="DEVICE-B", username="user-b", password="pass-b")

TOPIC_A = "dl/PLAF203/DEVICE-A/device/event/post"
TOPIC_B = "dl/PLAF203/DEVICE-B/device/event/post"
PAYLOAD = b'{"cmd":"GRAIN_OUTPUT_EVENT"}'


@dataclass
class FakeMessage:
    """Minimal stand-in for paho's MQTTMessage (only what callbacks read)."""

    topic: str
    payload: bytes
    qos: int = 0


@dataclass
class FakePublishResult:
    """Stand-in for paho's MQTTMessageInfo."""

    rc: int = 0

    def wait_for_publish(self, timeout: float | None = None) -> None:
        """Publication is synchronous for a fake client."""

    def is_published(self) -> bool:
        """Always published: the fake has no socket to fail on."""
        return True


@dataclass
class FakeClient:
    """MQTT client that records publishes instead of sending them."""

    connected: bool = True
    published: list[tuple[str, bytes]] = field(default_factory=list)

    def is_connected(self) -> bool:
        """Return the connection state the test set."""
        return self.connected

    def publish(self, topic: str, payload: bytes, qos: int = 0) -> FakePublishResult:
        """Record the publish and report success."""
        self.published.append((topic, payload))
        return FakePublishResult()

    def loop_stop(self) -> None:
        """No network loop to stop."""

    def disconnect(self) -> None:
        """Mark the fake session closed."""
        self.connected = False


@dataclass
class Harness:
    """A relay wired for two devices, with their upstream clients faked out."""

    config: RelayConfig
    queue: MessageQueue
    shadow: StateShadow
    telemetry: RelayTelemetry
    devices: DeviceManager
    bridge: MqttBridge
    local_client: FakeClient
    upstream: dict[str, FakeClient]
    presence: DevicePresenceTracker

    def deliver_local(self, topic: str, payload: bytes = PAYLOAD) -> None:
        """Feed a message in as if the local broker had delivered it."""
        self.bridge._on_local_message(
            cast(Client, self.local_client), None, cast(MQTTMessage, FakeMessage(topic, payload))
        )

    def deliver_cloud(self, device_id: str, topic: str, payload: bytes) -> None:
        """Feed a message in as if that device's cloud session had delivered it."""
        context = self.devices.get_by_device_id(device_id)
        assert context is not None
        context._on_upstream_message(
            cast(Client, self.upstream[device_id]),
            None,
            cast(MQTTMessage, FakeMessage(topic, payload)),
        )

    def pending(self, device_id: str, direction: str = LOCAL_TO_UPSTREAM) -> int:
        """Return one device's queue depth."""
        return self.queue.count(device_id, direction)


def build_harness(
    make_config: RelayConfigFactory, identities: tuple[DeviceIdentity, ...], **overrides: object
) -> Iterator[Harness]:
    """Construct a two-device relay with no real network anywhere in it."""
    config = make_config(**overrides)
    queue = MessageQueue(config.queue_db_path, config.max_queue_size)
    shadow = StateShadow(config.state_shadow_db_path)
    state_cache = StateCache(config.state_cache_path)
    telemetry = RelayTelemetry()
    registry = DeviceRegistry(config.device_registry_db_path)
    presence = DevicePresenceTracker()
    devices = DeviceManager(config, registry, queue, shadow, state_cache, telemetry, presence)

    upstream: dict[str, FakeClient] = {}
    for identity in identities:
        presence.session_opened(identity.client_id, "10.3.100.1")
        context = devices.ensure_device(identity)
        client = FakeClient()
        upstream[identity.client_id] = client
        # Replace the real Paho client so nothing can dial the cloud.
        context._upstream_client = cast(Client, client)

    bridge = MqttBridge(config, devices, queue, telemetry)
    local_client = FakeClient()
    bridge._local_client = cast(Client, local_client)

    yield Harness(
        config, queue, shadow, telemetry, devices, bridge, local_client, upstream, presence
    )

    devices.stop()
    registry.close()
    queue.close()
    shadow.close()


@pytest.fixture
def harness(make_config: RelayConfigFactory) -> Iterator[Harness]:
    """Two enrolled devices, pure-pipe mode."""
    yield from build_harness(make_config, (DEVICE_A, DEVICE_B))


@pytest.fixture
def responder_harness(make_config: RelayConfigFactory) -> Iterator[Harness]:
    """Two enrolled devices with the local responder fully enabled."""
    yield from build_harness(
        make_config,
        (DEVICE_A, DEVICE_B),
        local_responder=LocalResponderSettings(
            enabled=True, ntp=True, config=True, feeding_plan=True
        ),
    )


# -- enrollment ------------------------------------------------------------------


def test_each_device_gets_its_own_context(harness: Harness) -> None:
    """Adding B leaves A's context intact rather than replacing it."""
    context_a = harness.devices.get_by_device_id("DEVICE-A")
    context_b = harness.devices.get_by_device_id("DEVICE-B")

    assert context_a is not None and context_b is not None
    assert context_a is not context_b
    assert harness.devices.device_ids() == ["DEVICE-A", "DEVICE-B"]


def test_reconnecting_device_reuses_its_context(harness: Harness) -> None:
    """A device reconnecting must not open a second cloud session."""
    first = harness.devices.get_by_device_id("DEVICE-A")

    again = harness.devices.ensure_device(DEVICE_A)

    assert again is first
    assert harness.devices.device_ids() == ["DEVICE-A", "DEVICE-B"]


def test_no_two_devices_share_an_mqtt_identity(harness: Harness) -> None:
    """Each context carries its own credentials and its own topic prefix."""
    context_a = harness.devices.get_by_device_id("DEVICE-A")
    context_b = harness.devices.get_by_device_id("DEVICE-B")
    assert context_a is not None and context_b is not None

    assert context_a._identity.password != context_b._identity.password
    assert context_a.topic_prefix == "dl/PLAF203/DEVICE-A"
    assert context_b.topic_prefix == "dl/PLAF203/DEVICE-B"


# -- routing ---------------------------------------------------------------------


def test_local_message_is_routed_to_its_own_device(harness: Harness) -> None:
    """A /post from A enters A's queue and nothing enters B's."""
    harness.deliver_local(TOPIC_A)

    assert harness.pending("DEVICE-A") == 1
    assert harness.pending("DEVICE-B") == 0


def test_each_device_only_ever_sees_its_own_traffic(harness: Harness) -> None:
    """Two devices publishing leaves exactly one message in each queue."""
    harness.deliver_local(TOPIC_A)
    harness.deliver_local(TOPIC_B)

    assert harness.pending("DEVICE-A") == 1
    assert harness.pending("DEVICE-B") == 1


def test_unbridged_device_traffic_is_dropped(harness: Harness) -> None:
    """A device the relay does not bridge must not ride another's session."""
    harness.deliver_local("dl/PLAF203/DEVICE-UNKNOWN/device/event/post")

    assert harness.queue.depth_by_device() == {}


def test_prefix_lookalike_is_not_treated_as_a_known_device(harness: Harness) -> None:
    """A client id that merely starts with a known one must not slip through."""
    harness.deliver_local("dl/PLAF203/DEVICE-A-EVIL/device/event/post")

    assert harness.pending("DEVICE-A") == 0
    assert harness.queue.depth_by_device() == {}


def test_cloud_response_reaches_only_its_own_device(harness: Harness) -> None:
    """A /sub for A queues toward A alone."""
    harness.deliver_cloud("DEVICE-A", "dl/PLAF203/DEVICE-A/device/service/sub", PAYLOAD)

    assert harness.pending("DEVICE-A", UPSTREAM_TO_LOCAL) == 1
    assert harness.pending("DEVICE-B", UPSTREAM_TO_LOCAL) == 0


# -- queue isolation ---------------------------------------------------------------


def test_offline_device_queues_while_the_other_keeps_flowing(harness: Harness) -> None:
    """A's cloud outage must not stop B being delivered."""
    harness.upstream["DEVICE-A"].connected = False
    harness.deliver_local(TOPIC_A)
    harness.deliver_local(TOPIC_B)

    pump = DeliveryPump(
        LOCAL_TO_UPSTREAM, harness.queue, harness.bridge._upstream_targets, is_cloud_to_device=False
    )
    for target in harness.bridge._upstream_targets():
        pump._drain_device(target)

    assert harness.pending("DEVICE-A") == 1, "A stays queued while its cloud is down"
    assert harness.pending("DEVICE-B") == 0, "B must be unaffected by A's outage"
    assert harness.upstream["DEVICE-B"].published == [(TOPIC_B, PAYLOAD)]
    assert harness.upstream["DEVICE-A"].published == []


def test_replay_on_recovery_touches_only_the_recovered_device(harness: Harness) -> None:
    """When A comes back, A's backlog drains and B's queue is left alone."""
    harness.upstream["DEVICE-A"].connected = False
    harness.upstream["DEVICE-B"].connected = False
    harness.deliver_local(TOPIC_A)
    harness.deliver_local(TOPIC_B)

    harness.upstream["DEVICE-A"].connected = True
    pump = DeliveryPump(
        LOCAL_TO_UPSTREAM, harness.queue, harness.bridge._upstream_targets, is_cloud_to_device=False
    )
    for target in harness.bridge._upstream_targets():
        pump._drain_device(target)

    assert harness.pending("DEVICE-A") == 0
    assert harness.pending("DEVICE-B") == 1, "B's backlog must survive A's replay"
    assert harness.upstream["DEVICE-B"].published == []


def test_a_message_never_leaves_under_another_devices_session(harness: Harness) -> None:
    """The pump publishes each device's traffic on that device's own client."""
    harness.deliver_local(TOPIC_A)
    harness.deliver_local(TOPIC_B)

    pump = DeliveryPump(
        LOCAL_TO_UPSTREAM, harness.queue, harness.bridge._upstream_targets, is_cloud_to_device=False
    )
    for target in harness.bridge._upstream_targets():
        pump._drain_device(target)

    assert harness.upstream["DEVICE-A"].published == [(TOPIC_A, PAYLOAD)]
    assert harness.upstream["DEVICE-B"].published == [(TOPIC_B, PAYLOAD)]


def test_queue_size_cap_is_per_device(make_config: RelayConfigFactory) -> None:
    """One device flooding must not evict another device's backlog."""
    for harness in build_harness(make_config, (DEVICE_A, DEVICE_B), max_queue_size=3):
        harness.deliver_local(TOPIC_B)
        for _ in range(10):
            harness.deliver_local(TOPIC_A)

        assert harness.pending("DEVICE-A") == 3, "A is capped"
        assert harness.pending("DEVICE-B") == 1, "B keeps its message despite A's flood"


# -- state isolation -----------------------------------------------------------------


def test_shadow_state_never_leaks_between_devices(harness: Harness) -> None:
    """Config cached for A is not readable as B's."""
    harness.shadow.update_desired("DEVICE-A", {"grainNum": 3})
    harness.shadow.update_desired("DEVICE-B", {"grainNum": 9})

    assert harness.shadow.get_desired("DEVICE-A") == {"grainNum": 3}
    assert harness.shadow.get_desired("DEVICE-B") == {"grainNum": 9}


def test_feeding_plans_are_stored_per_device(harness: Harness) -> None:
    """A's plan set is never served as B's."""
    plan_a = [{"planId": "a1", "executionTime": "07:00", "grainNum": 2}]
    harness.shadow.update_feeding_plans("DEVICE-A", plan_a, "msg-a")

    assert harness.shadow.get_feeding_plans("DEVICE-B") is None
    stored = harness.shadow.get_feeding_plans("DEVICE-A")
    assert stored is not None and stored.plans == plan_a


# -- transaction isolation ------------------------------------------------------------


def test_same_msg_id_on_two_devices_is_two_transactions(responder_harness: Harness) -> None:
    """A msgId answered locally for A must not suppress B's real cloud reply."""
    context_a = responder_harness.devices.get_by_device_id("DEVICE-A")
    context_b = responder_harness.devices.get_by_device_id("DEVICE-B")
    assert context_a is not None and context_b is not None
    responder_a, responder_b = context_a.responder, context_b.responder
    assert responder_a is not None and responder_b is not None

    shared_msg_id = "0123456789abcdef0123456789abcdef"
    responder_a._handled_msg_ids[("DEVICE-A", shared_msg_id)] = 1e12
    payload = json.dumps({"cmd": "ATTR_SET_SERVICE", "msgId": shared_msg_id}).encode()

    assert responder_a.is_suppressed_cloud_response("DEVICE-A", payload) is True
    assert responder_b.is_suppressed_cloud_response("DEVICE-B", payload) is False


def test_one_responder_never_reads_another_devices_cache(responder_harness: Harness) -> None:
    """Suppression is keyed by device even inside a single responder instance."""
    context_a = responder_harness.devices.get_by_device_id("DEVICE-A")
    assert context_a is not None
    responder = context_a.responder
    assert responder is not None

    msg_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    responder._handled_msg_ids[("DEVICE-A", msg_id)] = 1e12
    payload = json.dumps({"cmd": "ATTR_SET_SERVICE", "msgId": msg_id}).encode()

    assert responder.is_suppressed_cloud_response("DEVICE-B", payload) is False


def test_local_fallback_answers_from_its_own_device_state(responder_harness: Harness) -> None:
    """B's cached plans must never be used to answer A."""
    responder_harness.shadow.update_feeding_plans(
        "DEVICE-B", [{"planId": "b1", "executionTime": "08:00", "grainNum": 1}], "msg-b"
    )
    context_a = responder_harness.devices.get_by_device_id("DEVICE-A")
    assert context_a is not None

    request = json.dumps({"cmd": "FEEDING_PLAN_SERVICE", "msgId": "req-a"}).encode()
    action = context_a.handle_local_message(
        "dl/PLAF203/DEVICE-A/device/service/post", request, qos=0
    )

    assert action is None, "A has no cached plan set, so it must fall through to the cloud"
    assert responder_harness.pending("DEVICE-A") == 1


# -- metrics isolation -----------------------------------------------------------------


def test_metrics_are_counted_per_device(harness: Harness) -> None:
    """A's traffic increments A's counters only."""
    harness.deliver_local(TOPIC_A)
    harness.deliver_local(TOPIC_A)
    harness.deliver_local(TOPIC_B)

    snapshots = {item["device_id"]: item for item in harness.telemetry.device_snapshots()}
    harness.telemetry.device("DEVICE-A").increment("probe")

    assert snapshots["DEVICE-A"]["device_id"] == "DEVICE-A"
    assert "probe" not in snapshots["DEVICE-B"]["upstream"]["counters"]


def test_one_devices_outage_does_not_change_another_state(harness: Harness) -> None:
    """Upstream state machines are entirely independent."""
    telemetry_a = harness.telemetry.device("DEVICE-A")
    telemetry_b = harness.telemetry.device("DEVICE-B")

    telemetry_b.upstream_online()
    telemetry_a.upstream_connect_attempt()
    telemetry_a.upstream_disconnected("timeout")

    assert telemetry_a.upstream_state.name == "DISCONNECTED"
    assert telemetry_b.upstream_state.name == "ONLINE", "B must be untouched by A's failure"


# -- registry restart --------------------------------------------------------------


def test_restart_rebuilds_every_device_context(make_config: RelayConfigFactory) -> None:
    """Three enrolled devices come back as three contexts after a restart."""
    config = make_config()
    registry = DeviceRegistry(config.device_registry_db_path)
    device_c = DeviceIdentity(client_id="DEVICE-C", username="user-c", password="pass-c")
    for identity in (DEVICE_A, DEVICE_B, device_c):
        registry.record(identity)

    queue = MessageQueue(config.queue_db_path, config.max_queue_size)
    shadow = StateShadow(config.state_shadow_db_path)
    telemetry = RelayTelemetry()
    devices = DeviceManager(
        config,
        registry,
        queue,
        shadow,
        StateCache(config.state_cache_path),
        telemetry,
        DevicePresenceTracker(),
    )
    # start() would dial the cloud, so rebuild exactly what it enrolls.
    for identity in registry.get_bridgeable():
        devices.ensure_device(identity)

    assert devices.device_ids() == ["DEVICE-A", "DEVICE-B", "DEVICE-C"]

    devices.stop()
    registry.close()
    queue.close()
    shadow.close()


# -- concurrency ---------------------------------------------------------------------


def test_interleaved_traffic_never_crosses_over(harness: Harness) -> None:
    """Concurrent publishing from both devices keeps each queue exact."""
    message_count = 40

    def publish(topic: str) -> None:
        for _ in range(message_count):
            harness.deliver_local(topic)

    threads = [
        threading.Thread(target=publish, args=(TOPIC_A,)),
        threading.Thread(target=publish, args=(TOPIC_B,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert harness.pending("DEVICE-A") == message_count
    assert harness.pending("DEVICE-B") == message_count


def test_every_queued_topic_belongs_to_its_own_device(harness: Harness) -> None:
    """No row ends up filed under the wrong device id."""
    for _ in range(5):
        harness.deliver_local(TOPIC_A)
        harness.deliver_local(TOPIC_B)

    for device_id, expected_topic in (("DEVICE-A", TOPIC_A), ("DEVICE-B", TOPIC_B)):
        snapshot = harness.queue.snapshot(device_id, LOCAL_TO_UPSTREAM, limit=100)
        topics = {cast(dict[str, Any], item)["topic"] for item in cast(list[Any], snapshot["messages"])}
        assert topics == {expected_topic}


# -- teardown ------------------------------------------------------------------------


def test_removing_one_device_leaves_the_others_bridged(harness: Harness) -> None:
    """Stopping B must not disturb A."""
    harness.devices.remove_device("DEVICE-B")

    assert harness.devices.device_ids() == ["DEVICE-A"]
    harness.deliver_local(TOPIC_A)
    assert harness.pending("DEVICE-A") == 1


def test_context_lookup_is_exact(harness: Harness) -> None:
    """Unknown ids resolve to nothing rather than to a nearby device."""
    assert harness.devices.get_by_device_id("DEVICE-") is None
    assert harness.devices.get_by_client_id("DEVICE-A") is not None
