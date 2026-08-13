"""Tests that a cloud session is only held while its device is actually here.

The relay authenticates upstream *as the device*. Keeping that session open
for a feeder that is powered off would tell PETLIBRO the device is online when
it cannot answer anything, so the session follows local presence:

    locally present            -> upstream connects and reconnects normally
    absent past the grace      -> upstream is closed, context stays loaded
    feeder returns             -> upstream restarts, its backlog replays

All of it is per device. One feeder disappearing must be invisible to the
others. No sockets are opened: the upstream clients are fakes.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import cast

import pytest
from paho.mqtt.client import Client, MQTTMessage

from conftest import RelayConfigFactory

from petlibro_relay.delivery_pump import DeliveryPump
from petlibro_relay.device_context import LOCAL_TO_UPSTREAM, DeviceContext
from petlibro_relay.device_manager import DeviceManager
from petlibro_relay.device_presence import DevicePresenceTracker, LocalPresence
from petlibro_relay.device_registry import DeviceIdentity, DeviceRegistry
from petlibro_relay.local_responder import UpstreamState
from petlibro_relay.message_queue import MessageQueue
from petlibro_relay.mqtt_bridge import MqttBridge
from petlibro_relay.observability.telemetry import RelayTelemetry
from petlibro_relay.state_cache import StateCache
from petlibro_relay.state_shadow import StateShadow

DEVICE_A = DeviceIdentity(client_id="DEVICE-A", username="user-a", password="pass-a")
DEVICE_B = DeviceIdentity(client_id="DEVICE-B", username="user-b", password="pass-b")
TOPIC_A = "dl/PLAF203/DEVICE-A/device/event/post"
TOPIC_B = "dl/PLAF203/DEVICE-B/device/event/post"
PAYLOAD = b'{"cmd":"GRAIN_OUTPUT_EVENT"}'

GRACE_SECONDS = 90.0


@dataclass
class ManualClock:
    """Controllable clock so the grace period can be crossed deterministically."""

    now: float = 1_000.0

    def __call__(self) -> float:
        """Return the current synthetic epoch."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move time forward."""
        self.now += seconds


@dataclass
class FakeMessage:
    """Minimal stand-in for paho's MQTTMessage."""

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
    """Records the session lifecycle instead of touching the network."""

    connected: bool = True
    published: list[tuple[str, bytes]] = field(default_factory=list)
    disconnect_calls: int = 0
    loop_stop_calls: int = 0

    def is_connected(self) -> bool:
        """Return whether this fake session is usable."""
        return self.connected

    def publish(self, topic: str, payload: bytes, qos: int = 0) -> FakePublishResult:
        """Record the publish and report success."""
        self.published.append((topic, payload))
        return FakePublishResult()

    def connect_async(self, host: str, port: int, keepalive: int = 60) -> None:
        """Accept the connect request without opening a socket."""

    def loop_start(self) -> None:
        """No network loop to run."""

    def loop_stop(self) -> None:
        """Record that the network loop was stopped."""
        self.loop_stop_calls += 1

    def disconnect(self) -> None:
        """Record a clean MQTT DISCONNECT."""
        self.disconnect_calls += 1
        self.connected = False


@dataclass
class Harness:
    """Two devices whose upstream clients are fakes, driven by a manual clock."""

    clock: ManualClock
    presence: DevicePresenceTracker
    devices: DeviceManager
    bridge: MqttBridge
    queue: MessageQueue
    built: dict[str, list[FakeClient]] = field(default_factory=dict)

    def context(self, device_id: str) -> DeviceContext:
        """Return a device's context, asserting it exists."""
        context = self.devices.get_by_device_id(device_id)
        assert context is not None
        return context

    def client(self, device_id: str) -> FakeClient | None:
        """Return the device's current fake upstream client, if any."""
        return cast(FakeClient | None, self.context(device_id).upstream_client)

    def clients_built(self, device_id: str) -> list[FakeClient]:
        """Return every fake client ever built for a device, oldest first."""
        return self.built.get(device_id, [])

    def deliver_local(self, topic: str, payload: bytes = PAYLOAD) -> None:
        """Feed in a message as if the local broker had delivered it."""
        self.bridge._on_local_message(
            cast(Client, None), None, cast(MQTTMessage, FakeMessage(topic, payload))
        )

    def drain(self) -> None:
        """Run one device-to-cloud pump pass over the current targets."""
        pump = DeliveryPump(
            LOCAL_TO_UPSTREAM, self.queue, self.bridge._upstream_targets, is_cloud_to_device=False
        )
        for target in self.bridge._upstream_targets():
            pump._drain_device(target)

    def pending(self, device_id: str) -> int:
        """Return a device's device-to-cloud queue depth."""
        return self.queue.count(device_id, LOCAL_TO_UPSTREAM)

    def go_offline(self, device_id: str) -> None:
        """End a device's local session and let the grace period expire."""
        self.presence.session_closed(device_id)
        self.clock.advance(GRACE_SECONDS + 1)
        self.devices.sync_upstream_sessions()

    def come_back(self, device_id: str) -> None:
        """Reopen a device's local session and reconcile."""
        self.presence.session_opened(device_id, "10.3.100.1")
        self.devices.sync_upstream_sessions()


@pytest.fixture
def harness(make_config: RelayConfigFactory) -> Iterator[Harness]:
    """Two devices, both locally present and both upstream, ready to diverge."""
    config = make_config()
    clock = ManualClock()
    queue = MessageQueue(config.queue_db_path, config.max_queue_size)
    shadow = StateShadow(config.state_shadow_db_path)
    registry = DeviceRegistry(config.device_registry_db_path)
    telemetry = RelayTelemetry()
    presence = DevicePresenceTracker(grace_seconds=GRACE_SECONDS, clock=clock)
    devices = DeviceManager(
        config,
        registry,
        queue,
        shadow,
        StateCache(config.state_cache_path),
        telemetry,
        presence,
        # Long enough that the supervisor thread never fires during a test:
        # reconciliation is driven explicitly so timing is deterministic.
        supervisor_interval_seconds=3600.0,
    )
    built: dict[str, list[FakeClient]] = {}

    # Substitute the client factory so start_upstream never dials the cloud
    # while still exercising the real lifecycle code around it.
    for identity in (DEVICE_A, DEVICE_B):
        presence.session_opened(identity.client_id, "10.3.100.1")
        registry.record(identity)
        context = devices.ensure_device(identity)
        history: list[FakeClient] = []
        built[identity.client_id] = history

        def factory(_history: list[FakeClient] = history) -> Client:
            client = FakeClient()
            _history.append(client)
            return cast(Client, client)

        context._build_upstream_client = factory  # type: ignore[method-assign]

    bridge = MqttBridge(config, devices, queue, telemetry)
    devices.start()

    yield Harness(clock, presence, devices, bridge, queue, built)

    devices.stop()
    registry.close()
    queue.close()
    shadow.close()


# -- baseline --------------------------------------------------------------------


def test_present_devices_hold_a_cloud_session(harness: Harness) -> None:
    """Both devices are here, so both have an upstream client running."""
    assert harness.context("DEVICE-A").upstream_running is True
    assert harness.context("DEVICE-B").upstream_running is True


def test_a_brief_local_drop_does_not_close_the_cloud_session(harness: Harness) -> None:
    """A feeder reconnecting within the grace must not churn its cloud session."""
    original = harness.client("DEVICE-B")

    harness.presence.session_closed("DEVICE-B")
    harness.clock.advance(GRACE_SECONDS / 2)
    harness.devices.sync_upstream_sessions()

    assert harness.presence.state("DEVICE-B") is LocalPresence.ONLINE
    assert harness.client("DEVICE-B") is original, "the session must survive a short blip"
    assert len(harness.clients_built("DEVICE-B")) == 1


# -- A online / B offline ----------------------------------------------------------


def test_absent_device_loses_its_cloud_session_after_the_grace(harness: Harness) -> None:
    """B powered off stops being represented to PETLIBRO as online."""
    original = harness.client("DEVICE-B")
    assert original is not None

    harness.go_offline("DEVICE-B")

    assert harness.presence.state("DEVICE-B") is LocalPresence.OFFLINE
    assert harness.context("DEVICE-B").upstream_running is False
    assert original.disconnect_calls == 1, "a clean DISCONNECT, not a dropped socket"
    assert original.loop_stop_calls == 1, "no Paho thread may be left behind"


def test_absent_device_does_not_disturb_the_present_one(harness: Harness) -> None:
    """A stays connected and untouched while B goes away."""
    original_a = harness.client("DEVICE-A")

    harness.go_offline("DEVICE-B")

    assert harness.context("DEVICE-A").upstream_running is True
    assert harness.client("DEVICE-A") is original_a, "A's session object must be unchanged"
    assert cast(FakeClient, original_a).disconnect_calls == 0
    assert len(harness.clients_built("DEVICE-A")) == 1


def test_suspension_is_reported_as_intentional_not_as_a_cloud_fault(harness: Harness) -> None:
    """A deliberately closed session is SUSPENDED, with no outage accruing."""
    harness.go_offline("DEVICE-B")

    telemetry = harness.context("DEVICE-B").telemetry
    upstream = telemetry.snapshot()["upstream"]

    assert telemetry.upstream_state is UpstreamState.SUSPENDED
    assert upstream["state"] == "SUSPENDED"
    assert upstream["outage"]["downtime_seconds"] is None, "suspension is not downtime"
    assert upstream["counters"]["upstream_suspensions"] == 1
    assert upstream["counters"].get("sessions_lost", 0) == 0, "not a lost session"


def test_absent_device_keeps_its_context_and_state_loaded(harness: Harness) -> None:
    """Only the socket goes away: the context and its queue stay put."""
    harness.deliver_local(TOPIC_B)
    harness.go_offline("DEVICE-B")

    assert harness.devices.get_by_device_id("DEVICE-B") is not None
    assert harness.devices.device_ids() == ["DEVICE-A", "DEVICE-B"]
    assert harness.pending("DEVICE-B") == 1, "the backlog must be retained, not discarded"


def test_absent_device_backlog_is_not_drained_to_anyone(harness: Harness) -> None:
    """With no session, B's messages wait - and never leave via A."""
    harness.go_offline("DEVICE-B")
    harness.deliver_local(TOPIC_B)
    harness.deliver_local(TOPIC_A)

    harness.drain()

    assert harness.pending("DEVICE-B") == 1, "B's traffic waits for B's own session"
    assert harness.pending("DEVICE-A") == 0
    published_a = cast(FakeClient, harness.client("DEVICE-A")).published
    assert published_a == [(TOPIC_A, PAYLOAD)], "B's message must never ride A's session"


# -- B reconnects ------------------------------------------------------------------


def test_returning_device_gets_a_fresh_cloud_session(harness: Harness) -> None:
    """B coming back restarts its own upstream client."""
    harness.go_offline("DEVICE-B")
    assert harness.context("DEVICE-B").upstream_running is False

    harness.come_back("DEVICE-B")

    assert harness.context("DEVICE-B").upstream_running is True
    assert len(harness.clients_built("DEVICE-B")) == 2, "a fresh client, not a revived one"


def test_returning_device_replays_only_its_own_backlog(harness: Harness) -> None:
    """B's queued traffic drains on reconnect; A's queue is untouched."""
    harness.go_offline("DEVICE-B")
    harness.deliver_local(TOPIC_B)
    harness.deliver_local(TOPIC_B)
    # A is offline too at this point, so its own backlog can build up.
    harness.go_offline("DEVICE-A")
    harness.deliver_local(TOPIC_A)

    harness.come_back("DEVICE-B")
    harness.drain()

    assert harness.pending("DEVICE-B") == 0, "B's backlog replays on its return"
    assert harness.pending("DEVICE-A") == 1, "A's backlog must not be touched by B's replay"
    replayed = cast(FakeClient, harness.client("DEVICE-B")).published
    assert replayed == [(TOPIC_B, PAYLOAD), (TOPIC_B, PAYLOAD)]
    assert harness.client("DEVICE-A") is None


def test_returning_device_does_not_restart_the_other(harness: Harness) -> None:
    """B's reconnect leaves A's still-running session exactly as it was."""
    original_a = harness.client("DEVICE-A")
    harness.go_offline("DEVICE-B")

    harness.come_back("DEVICE-B")

    assert harness.client("DEVICE-A") is original_a
    assert len(harness.clients_built("DEVICE-A")) == 1


def test_reconnect_cycles_do_not_leak_sessions(harness: Harness) -> None:
    """Repeated away/back cycles leave exactly one live client each time."""
    for _ in range(3):
        harness.go_offline("DEVICE-B")
        assert harness.context("DEVICE-B").upstream_running is False
        harness.come_back("DEVICE-B")
        assert harness.context("DEVICE-B").upstream_running is True

    built = harness.clients_built("DEVICE-B")
    assert len(built) == 4, "one initial client plus one per return"
    assert [client.connected for client in built[:-1]] == [False, False, False]


# -- idempotency and races -----------------------------------------------------------


def test_reconciling_repeatedly_changes_nothing(harness: Harness) -> None:
    """The supervisor can tick as often as it likes without churning sessions."""
    original_a = harness.client("DEVICE-A")
    harness.go_offline("DEVICE-B")

    for _ in range(5):
        harness.devices.sync_upstream_sessions()

    assert harness.client("DEVICE-A") is original_a
    assert len(harness.clients_built("DEVICE-A")) == 1
    assert len(harness.clients_built("DEVICE-B")) == 1, "no repeated restarts while absent"
    assert harness.context("DEVICE-B").upstream_running is False


def test_a_stale_callback_cannot_touch_the_restarted_session(harness: Harness) -> None:
    """A late Paho callback from a closed session is ignored, not acted on."""
    harness.go_offline("DEVICE-B")
    stale = harness.clients_built("DEVICE-B")[0]
    harness.come_back("DEVICE-B")
    context = harness.context("DEVICE-B")

    context._on_upstream_message(
        cast(Client, stale),
        None,
        cast(MQTTMessage, FakeMessage("dl/PLAF203/DEVICE-B/device/service/sub", PAYLOAD)),
    )

    assert harness.queue.count("DEVICE-B", "upstream-to-local") == 0, (
        "a replaced session must not be able to enqueue"
    )


def test_startup_does_not_connect_for_an_absent_known_device(
    make_config: RelayConfigFactory,
) -> None:
    """A relay restart must not impersonate a feeder that is powered off."""
    config = make_config()
    registry = DeviceRegistry(config.device_registry_db_path)
    registry.record(DEVICE_A)
    queue = MessageQueue(config.queue_db_path, config.max_queue_size)
    shadow = StateShadow(config.state_shadow_db_path)
    devices = DeviceManager(
        config,
        registry,
        queue,
        shadow,
        StateCache(config.state_cache_path),
        RelayTelemetry(),
        DevicePresenceTracker(),
        supervisor_interval_seconds=3600.0,
    )
    try:
        devices.start()

        context = devices.get_by_device_id("DEVICE-A")
        assert context is not None, "the device is still restored and bridgeable"
        assert context.upstream_running is False, (
            "no cloud session until the feeder is seen locally"
        )
    finally:
        devices.stop()
        registry.close()
        queue.close()
        shadow.close()


def test_device_connecting_during_startup_is_not_left_without_a_session(
    make_config: RelayConfigFactory,
) -> None:
    """A feeder that connects while its context is being restored still starts.

    This is the boot race: the capture proxy accepts before `DeviceManager`
    has started, so presence is recorded first and enrollment second.
    """
    config = make_config()
    registry = DeviceRegistry(config.device_registry_db_path)
    registry.record(DEVICE_A)
    queue = MessageQueue(config.queue_db_path, config.max_queue_size)
    shadow = StateShadow(config.state_shadow_db_path)
    presence = DevicePresenceTracker()
    devices = DeviceManager(
        config,
        registry,
        queue,
        shadow,
        StateCache(config.state_cache_path),
        RelayTelemetry(),
        presence,
        supervisor_interval_seconds=3600.0,
    )
    built: list[FakeClient] = []
    try:
        # The proxy sees the CONNECT before the manager has started.
        presence.session_opened("DEVICE-A", "10.3.100.1")
        context = devices.ensure_device(DEVICE_A)

        def factory() -> Client:
            client = FakeClient()
            built.append(client)
            return cast(Client, client)

        context._build_upstream_client = factory  # type: ignore[method-assign]
        assert context.upstream_running is False, "nothing starts before the manager does"

        devices.start()

        assert context.upstream_running is True, (
            "a device learned before startup must still get its cloud session"
        )
        assert len(built) == 1, "and exactly one, not a duplicate per code path"
    finally:
        devices.stop()
        registry.close()
        queue.close()
        shadow.close()
