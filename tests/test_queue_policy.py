"""Device-to-cloud queue policies during PETLIBRO upstream outages."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from conftest import RelayConfigFactory
from petlibro_relay.device_context import LOCAL_TO_UPSTREAM, DeviceContext
from petlibro_relay.local_responder import UpstreamState
from petlibro_relay.replay_policy import (
    DURABLE_EVENT_TTL_SECONDS,
    NEVER_REPLAY_GRACE_SECONDS,
    coalesce_key_for,
    policy_for,
)

from test_multi_device_isolation import (
    DEVICE_A,
    DEVICE_B,
    TOPIC_A,
    TOPIC_B,
    Harness,
    build_harness,
)


@pytest.fixture
def harness(make_config: RelayConfigFactory) -> Iterator[Harness]:
    """Provide two isolated devices with fake local and upstream clients."""
    yield from build_harness(make_config, (DEVICE_A, DEVICE_B))


def _payload(command: str, **fields: object) -> bytes:
    """Build one fake device report without touching a broker."""
    return json.dumps({"cmd": command, **fields}).encode()


def _context(harness: Harness, device_id: str) -> DeviceContext:
    """Return a bridged test context."""
    context = harness.devices.get_by_device_id(device_id)
    assert context is not None
    return context


def test_ephemeral_heartbeats_and_ntp_do_not_enter_queue_during_outage(harness: Harness) -> None:
    """A long outage must not turn session chatter into a durable backlog."""
    for _ in range(500):
        harness.deliver_local(TOPIC_A, _payload("HEARTBEAT"))
    for _ in range(10):
        harness.deliver_local(TOPIC_A.replace("/event/", "/ntp/"), _payload("NTP"))

    context = _context(harness, DEVICE_A.client_id)
    assert context.upstream_state is UpstreamState.DISCONNECTED
    assert harness.pending(DEVICE_A.client_id) == 0
    assert context.telemetry.snapshot()["upstream"]["counters"]["queue_dropped_ephemeral"] == 510


def test_heartbeat_is_enqueued_when_upstream_is_online(harness: Harness) -> None:
    """Ephemeral reports are still forwarded normally on a live session."""
    context = _context(harness, DEVICE_A.client_id)
    context.telemetry.upstream_online()

    harness.deliver_local(TOPIC_A, _payload("HEARTBEAT"))

    queued = harness.queue.peek_oldest(DEVICE_A.client_id, LOCAL_TO_UPSTREAM)
    assert queued is not None
    assert queued.max_age_seconds == NEVER_REPLAY_GRACE_SECONDS


def test_durable_events_and_latest_state_survive_outage(harness: Harness) -> None:
    """Real events remain FIFO while state reports coalesce to one newest row."""
    for command in ("GRAIN_OUTPUT_EVENT", "ERROR_EVENT", "DETECTION_EVENT"):
        harness.deliver_local(TOPIC_A, _payload(command))
    harness.deliver_local(TOPIC_A.replace("/event/", "/service/"), _payload("ATTR_SET_SERVICE", code=0))
    harness.deliver_local(TOPIC_A.replace("/event/", "/service/"), _payload("ATTR_SET_SERVICE", code=0))

    assert harness.pending(DEVICE_A.client_id) == 5
    messages = []
    while message := harness.queue.peek_oldest(DEVICE_A.client_id, LOCAL_TO_UPSTREAM):
        messages.append(message)
        harness.queue.remove(message.id)
    assert [json.loads(message.payload)["cmd"] for message in messages] == [
        "GRAIN_OUTPUT_EVENT",
        "ERROR_EVENT",
        "DETECTION_EVENT",
        "ATTR_SET_SERVICE",
        "ATTR_SET_SERVICE",
    ]
    assert all(message.max_age_seconds == DURABLE_EVENT_TTL_SECONDS for message in messages[:3])
    assert all(message.max_age_seconds is None for message in messages[3:])


def test_attr_set_coalesces_per_modified_setting(harness: Harness) -> None:
    """Different ATTR_SET settings must never evict each other from a backlog."""
    service_topic = TOPIC_A.replace("/event/", "/service/")
    sound_enabled = _payload("ATTR_SET_SERVICE", soundSwitch=True)
    motion_disabled = _payload("ATTR_SET_SERVICE", motionDetectionSwitch=False)
    sound_disabled = _payload("ATTR_SET_SERVICE", soundSwitch=False)
    policy = policy_for(False, "ATTR_SET_SERVICE")

    assert coalesce_key_for(service_topic, "ATTR_SET_SERVICE", policy, sound_enabled) == (
        f"{service_topic}|ATTR_SET_SERVICE|soundSwitch"
    )
    assert coalesce_key_for(service_topic, "ATTR_SET_SERVICE", policy, motion_disabled) == (
        f"{service_topic}|ATTR_SET_SERVICE|motionDetectionSwitch"
    )
    assert coalesce_key_for(
        service_topic,
        "ATTR_SET_SERVICE",
        policy,
        _payload("ATTR_SET_SERVICE", msgId="ack", code=0),
    ) is None

    harness.deliver_local(service_topic, sound_enabled)
    harness.deliver_local(service_topic, motion_disabled)
    assert harness.pending(DEVICE_A.client_id) == 2

    harness.deliver_local(service_topic, sound_disabled)
    assert harness.pending(DEVICE_A.client_id) == 2
    queued_payloads = []
    while message := harness.queue.peek_oldest(DEVICE_A.client_id, LOCAL_TO_UPSTREAM):
        queued_payloads.append(json.loads(message.payload))
        harness.queue.remove(message.id)
    values_by_setting = {
        setting: body[setting]
        for body in queued_payloads
        for setting in ("soundSwitch", "motionDetectionSwitch")
        if setting in body
    }
    assert values_by_setting == {"soundSwitch": False, "motionDetectionSwitch": False}


def test_unknown_device_report_keeps_conservative_durable_fifo(harness: Harness) -> None:
    """Unrecognised protocol traffic is preserved rather than silently lost."""
    harness.deliver_local(TOPIC_A, _payload("FUTURE_FIRMWARE_EVENT"))

    queued = harness.queue.peek_oldest(DEVICE_A.client_id, LOCAL_TO_UPSTREAM)
    assert queued is not None
    assert queued.max_age_seconds is None


def test_outage_policy_remains_isolated_per_device(harness: Harness) -> None:
    """One device's heartbeat drop cannot affect another device's event queue."""
    harness.deliver_local(TOPIC_A, _payload("HEARTBEAT"))
    harness.deliver_local(TOPIC_B, _payload("ERROR_EVENT"))

    assert harness.pending(DEVICE_A.client_id) == 0
    assert harness.pending(DEVICE_B.client_id) == 1


def test_sound_switch_ack_remains_queueable_during_outage(harness: Harness) -> None:
    """The natural feeder ACK remains available for later cloud forwarding."""
    service_topic = TOPIC_A.replace("/event/", "/service/")
    harness.deliver_local(
        service_topic,
        _payload("ATTR_SET_SERVICE", code=0, msgId="sound-switch-ack", soundSwitch=True),
    )

    queued = harness.queue.peek_oldest(DEVICE_A.client_id, LOCAL_TO_UPSTREAM)
    assert queued is not None
    assert json.loads(queued.payload)["msgId"] == "sound-switch-ack"
    assert queued.max_age_seconds is None
