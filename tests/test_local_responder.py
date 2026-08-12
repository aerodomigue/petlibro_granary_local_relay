"""Tests for the local responder and its state shadow.

Payloads here are the real ones captured from this project's PLAF203 on
firmware V3.0.30, not invented shapes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from petlibro_relay.local_responder import (
    Decision,
    LocalResponder,
    LocalResponderSettings,
    UpstreamState,
)
from petlibro_relay.state_shadow import StateShadow

DEVICE_ID = "TESTDEVICE0000000001"
PREFIX = f"dl/PLAF203/{DEVICE_ID}"
NTP_POST = f"{PREFIX}/device/ntp/post"
SERVICE_POST = f"{PREFIX}/device/service/post"
SERVICE_SUB = f"{PREFIX}/device/service/sub"

MSG_ID = "d030bf2039514327835b186c254d9eeb"

# Captured verbatim from the real device / cloud.
NTP_REQUEST = json.dumps({"cmd": "NTP", "ts": 1786541894000}).encode()
CLOUD_PLANS: list[dict[str, Any]] = [
    {
        "planId": 5784463,
        "executionTime": "16:30",
        "repeatDay": [7, 1, 2, 3, 4, 5, 6],
        "enableAudio": False,
        "audioTimes": 2,
        "grainNum": 1,
        "syncTime": 1786530227000,
    },
    {
        "planId": 5784461,
        "executionTime": "07:30",
        "repeatDay": [7, 1, 2, 3, 4, 5, 6],
        "enableAudio": False,
        "audioTimes": 2,
        "grainNum": 3,
        "syncTime": 1786445361000,
    },
]
CLOUD_PLAN_PUSH = json.dumps(
    {"cmd": "FEEDING_PLAN_SERVICE", "ts": 1786541474944, "msgId": MSG_ID, "plans": CLOUD_PLANS}
).encode()
# The device's own post names plan ids only - no schedule.
DEVICE_PLAN_REQUEST = json.dumps(
    {
        "cmd": "FEEDING_PLAN_SERVICE",
        "code": 0,
        "msgId": MSG_ID,
        "plans": [{"planId": 5784463, "syncTime": 1786530227000}],
        "ts": 1786541473000,
    }
).encode()
CLOUD_SETTINGS_PUSH = json.dumps(
    {"cmd": "ATTR_SET_SERVICE", "ts": 1786532839015, "msgId": "abc", "soundSwitch": True, "soundAgingType": 1}
).encode()
CONFIG_REQUEST = json.dumps({"cmd": "ATTR_GET_SERVICE", "msgId": "cfg-1", "ts": 1}).encode()


def make_settings(**overrides: Any) -> LocalResponderSettings:
    """Fully-enabled responder settings, Europe/Paris, with per-test overrides."""
    base = {
        "enabled": True,
        "ntp": True,
        "config": True,
        "feeding_plan": True,
        "always_answer_ntp_locally": False,
        "device_timezone": "Europe/Paris",
    }
    base.update(overrides)
    return LocalResponderSettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def shadow(tmp_path: Path) -> Iterator[StateShadow]:
    """A fresh persistent state shadow."""
    instance = StateShadow(str(tmp_path / "shadow.sqlite3"))
    yield instance
    instance.close()


@pytest.fixture
def responder(shadow: StateShadow) -> LocalResponder:
    """A responder with every local function enabled."""
    return LocalResponder(make_settings(), shadow, handled_msg_id_ttl_seconds=120.0)


def decode(payload: bytes | None) -> dict[str, Any]:
    assert payload is not None
    result: dict[str, Any] = json.loads(payload)
    return result


def test_ntp_answered_locally_when_upstream_is_down(responder: LocalResponder) -> None:
    """NTP offline: a valid NTP_SYNC is generated with a fresh msgId and timestamp."""
    action = responder.decide(DEVICE_ID, NTP_POST, NTP_REQUEST, UpstreamState.DISCONNECTED)

    assert action.decision is Decision.RESPOND_LOCAL
    assert action.response_topic == f"{PREFIX}/device/ntp/sub"
    body = decode(action.response_payload)
    assert body["cmd"] == "NTP_SYNC"
    assert len(body["msgId"]) == 32, "cloud-style 32-char hex msgId"
    assert body["ts"] > 1_700_000_000_000, "epoch milliseconds"
    # Field set matches what the real V3.0.30 cloud sends.
    assert body["timezoneOffsetSeconds"] == 7200
    assert body["timezone"] == 2
    assert "nextDSTOffsetSeconds" in body
    assert "nextDSTTransitionTs" in body


def test_ntp_stays_transparent_while_upstream_is_online(responder: LocalResponder) -> None:
    """NTP online: the cloud keeps answering, the relay only observes."""
    action = responder.decide(DEVICE_ID, NTP_POST, NTP_REQUEST, UpstreamState.ONLINE)

    assert action.decision is Decision.CACHE_AND_FORWARD
    assert action.response_payload is None


def test_ntp_can_be_forced_local_even_when_online(shadow: StateShadow) -> None:
    """The opt-in flag answers NTP locally regardless of upstream state."""
    responder = LocalResponder(
        make_settings(always_answer_ntp_locally=True), shadow, handled_msg_id_ttl_seconds=120.0
    )

    action = responder.decide(DEVICE_ID, NTP_POST, NTP_REQUEST, UpstreamState.ONLINE)

    assert action.decision is Decision.RESPOND_LOCAL


def test_tcp_connecting_is_not_treated_as_online(responder: LocalResponder) -> None:
    """A half-open connection must not count as the cloud being available."""
    for state in (UpstreamState.TCP_CONNECTING, UpstreamState.MQTT_CONNECTING):
        action = responder.decide(DEVICE_ID, NTP_POST, NTP_REQUEST, state)
        assert action.decision is Decision.RESPOND_LOCAL, f"{state} must not count as online"


def test_feeding_plan_served_from_last_known_good(responder: LocalResponder) -> None:
    """The cloud's plan set is learned, then replayed when the cloud is gone."""
    responder.observe_cloud_message(DEVICE_ID, SERVICE_SUB, CLOUD_PLAN_PUSH)

    action = responder.decide(DEVICE_ID, SERVICE_POST, DEVICE_PLAN_REQUEST, UpstreamState.DISCONNECTED)

    assert action.decision is Decision.RESPOND_LOCAL
    body = decode(action.response_payload)
    assert body["cmd"] == "FEEDING_PLAN_SERVICE"
    assert body["msgId"] == MSG_ID, "must answer the request's own msgId"
    assert body["plans"] == CLOUD_PLANS
    assert "grainNum" in body["plans"][0], "a plan definition, not a feed command"


def test_device_plan_stub_never_overwrites_the_real_plans(responder: LocalResponder) -> None:
    """The device's id-only plan list must not be mistaken for a plan definition."""
    responder.observe_cloud_message(DEVICE_ID, SERVICE_SUB, CLOUD_PLAN_PUSH)
    responder.observe_cloud_message(DEVICE_ID, SERVICE_SUB, DEVICE_PLAN_REQUEST)

    action = responder.decide(DEVICE_ID, SERVICE_POST, DEVICE_PLAN_REQUEST, UpstreamState.DISCONNECTED)
    assert decode(action.response_payload)["plans"] == CLOUD_PLANS


def test_config_served_from_cache(responder: LocalResponder) -> None:
    """Config cache: settings pushed by the cloud come back when it is unreachable."""
    responder.observe_cloud_message(DEVICE_ID, SERVICE_SUB, CLOUD_SETTINGS_PUSH)

    action = responder.decide(DEVICE_ID, SERVICE_POST, CONFIG_REQUEST, UpstreamState.DISCONNECTED)

    assert action.decision is Decision.RESPOND_LOCAL
    body = decode(action.response_payload)
    assert body["msgId"] == "cfg-1"
    assert body["soundSwitch"] is True
    assert body["soundAgingType"] == 1


def test_config_miss_invents_nothing(responder: LocalResponder) -> None:
    """Config miss: with an empty cache, no answer is fabricated."""
    action = responder.decide(DEVICE_ID, SERVICE_POST, CONFIG_REQUEST, UpstreamState.DISCONNECTED)

    assert action.decision is Decision.CACHE_AND_FORWARD
    assert action.response_payload is None
    assert responder.counters.cache_misses == 1


def test_unknown_command_gets_no_local_answer(responder: LocalResponder) -> None:
    """Unknown command: forwarded, never answered from guesswork."""
    payload = json.dumps({"cmd": "SOME_FUTURE_COMMAND", "msgId": "x"}).encode()

    action = responder.decide(DEVICE_ID, SERVICE_POST, payload, UpstreamState.DISCONNECTED)

    assert action.decision is Decision.CACHE_AND_FORWARD
    assert action.response_payload is None
    assert responder.counters.unknown_requests == 1


def test_dangerous_commands_are_never_answered_locally(responder: LocalResponder) -> None:
    """Physical-action commands are never synthesised, even with a full cache."""
    responder.observe_cloud_message(DEVICE_ID, SERVICE_SUB, CLOUD_PLAN_PUSH)
    responder.observe_cloud_message(DEVICE_ID, SERVICE_SUB, CLOUD_SETTINGS_PUSH)

    for command in ("MANUAL_FEEDING_SERVICE", "DEVICE_REBOOT", "RESET", "RESTORE"):
        payload = json.dumps({"cmd": command, "msgId": "danger", "grainNum": 5}).encode()
        action = responder.decide(DEVICE_ID, SERVICE_POST, payload, UpstreamState.DISCONNECTED)
        assert action.decision is Decision.CACHE_AND_FORWARD, f"{command} must never be answered"
        assert action.response_payload is None


def test_late_cloud_response_is_suppressed(responder: LocalResponder) -> None:
    """Double-response protection: the device must not get two answers to one request."""
    responder.observe_cloud_message(DEVICE_ID, SERVICE_SUB, CLOUD_PLAN_PUSH)
    action = responder.decide(DEVICE_ID, SERVICE_POST, DEVICE_PLAN_REQUEST, UpstreamState.DISCONNECTED)
    assert action.decision is Decision.RESPOND_LOCAL

    # The cloud comes back and answers the same msgId.
    assert responder.is_suppressed_cloud_response(CLOUD_PLAN_PUSH) is True
    assert responder.counters.suppressed_late_cloud_responses == 1


def test_unrelated_cloud_response_is_not_suppressed(responder: LocalResponder) -> None:
    """Suppression must be scoped to the exact msgId that was answered locally."""
    responder.observe_cloud_message(DEVICE_ID, SERVICE_SUB, CLOUD_PLAN_PUSH)
    responder.decide(DEVICE_ID, SERVICE_POST, DEVICE_PLAN_REQUEST, UpstreamState.DISCONNECTED)

    other = json.dumps({"cmd": "ATTR_SET_SERVICE", "msgId": "different", "soundSwitch": False}).encode()
    assert responder.is_suppressed_cloud_response(other) is False


def test_state_survives_a_restart(tmp_path: Path) -> None:
    """Restart: the shadow is persistent, so last-known-good is still usable."""
    db = str(tmp_path / "shadow.sqlite3")

    first = StateShadow(db)
    LocalResponder(make_settings(), first, 120.0).observe_cloud_message(
        DEVICE_ID, SERVICE_SUB, CLOUD_PLAN_PUSH
    )
    first.close()

    reopened = StateShadow(db)
    responder = LocalResponder(make_settings(), reopened, 120.0)
    action = responder.decide(DEVICE_ID, SERVICE_POST, DEVICE_PLAN_REQUEST, UpstreamState.DISCONNECTED)
    reopened.close()

    assert action.decision is Decision.RESPOND_LOCAL
    assert decode(action.response_payload)["plans"] == CLOUD_PLANS


def test_disabled_responder_is_a_pure_pipe(shadow: StateShadow) -> None:
    """With the feature off, nothing is ever answered locally."""
    responder = LocalResponder(LocalResponderSettings(), shadow, 120.0)

    action = responder.decide(DEVICE_ID, NTP_POST, NTP_REQUEST, UpstreamState.DISCONNECTED)

    assert action.decision is Decision.CACHE_AND_FORWARD
    assert action.response_payload is None


def test_device_reported_state_is_recorded(responder: LocalResponder, shadow: StateShadow) -> None:
    """Heartbeats populate the reported side of the shadow, verbatim."""
    heartbeat = json.dumps(
        {"cmd": "HEARTBEAT", "count": 34, "rssi": -43, "wifiType": 1, "ts": 1786542361000}
    ).encode()

    responder.decide(DEVICE_ID, f"{PREFIX}/device/heart/post", heartbeat, UpstreamState.ONLINE)

    reported = shadow.get_reported(DEVICE_ID)
    assert reported["rssi"] == -43
    assert reported["last_heartbeat_ts"] == 1786542361000
