"""Tests for opt-in PETLIBRO cloud service-payload diagnostics."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from conftest import RelayConfigFactory
from test_multi_device_isolation import DEVICE_A, TOPIC_A, Harness, build_harness

from petlibro_relay.device_context import UPSTREAM_TO_LOCAL


def _service_sub_topic() -> str:
    """Return the service command topic for the isolated fake device."""
    return TOPIC_A.replace("/event/post", "/service/sub")


@pytest.fixture
def disabled_harness(make_config: RelayConfigFactory) -> Iterator[Harness]:
    """Provide a fake device context using the default disabled diagnostic."""
    yield from build_harness(make_config, (DEVICE_A,))


@pytest.fixture
def enabled_harness(make_config: RelayConfigFactory) -> Iterator[Harness]:
    """Provide a fake device context with cloud diagnostics explicitly enabled."""
    yield from build_harness(
        make_config,
        (DEVICE_A,),
        log_upstream_service_payloads=True,
    )


def test_service_payload_logging_is_quiet_when_flag_is_disabled(
    disabled_harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """The default flag adds no INFO payload record to the normal relay flow."""
    payload = json.dumps({"cmd": "ATTR_SET_SERVICE", "msgId": "abc"}).encode()

    with caplog.at_level(logging.INFO, logger="petlibro_relay.device_context"):
        disabled_harness.deliver_cloud(DEVICE_A.client_id, _service_sub_topic(), payload)

    assert "UPSTREAM SERVICE RX" not in caplog.text
    queued = disabled_harness.queue.peek_oldest(DEVICE_A.client_id, UPSTREAM_TO_LOCAL)
    assert queued is not None
    assert queued.payload == payload


def test_enabled_service_payload_log_shows_functional_setting_fields(
    enabled_harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """A cloud ATTR_SET is logged before its unchanged payload enters the queue."""
    payload = json.dumps(
        {"cmd": "ATTR_SET_SERVICE", "msgId": "abc", "motionDetectionSwitch": False}
    ).encode()

    with caplog.at_level(logging.INFO, logger="petlibro_relay.device_context"):
        enabled_harness.deliver_cloud(DEVICE_A.client_id, _service_sub_topic(), payload)

    assert "UPSTREAM SERVICE RX source=petlibro-cloud" in caplog.text
    assert "cmd=ATTR_SET_SERVICE" in caplog.text
    assert "msgId=abc" in caplog.text
    assert '"motionDetectionSwitch":false' in caplog.text
    queued = enabled_harness.queue.peek_oldest(DEVICE_A.client_id, UPSTREAM_TO_LOCAL)
    assert queued is not None
    assert queued.payload == payload


def test_enabled_service_payload_log_handles_non_json_without_an_exception(
    enabled_harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed payloads remain forwardable and have a bounded diagnostic form."""
    payload = b"not-json"

    with caplog.at_level(logging.INFO, logger="petlibro_relay.device_context"):
        enabled_harness.deliver_cloud(DEVICE_A.client_id, _service_sub_topic(), payload)

    assert "cmd=unknown msgId=none payload=<8 bytes non-json>" in caplog.text


def test_enabled_service_payload_log_redacts_connection_and_tutk_secrets(
    enabled_harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """Opt-in diagnostics never leak credentials from an unexpected payload."""
    payload = json.dumps(
        {
            "cmd": "ATTR_SET_SERVICE",
            "msgId": "safe-id",
            "soundSwitch": True,
            "username": "cloud-user",
            "password": "mqtt-password",
            "tutkToken": "p2p-secret",
        }
    ).encode()

    with caplog.at_level(logging.INFO, logger="petlibro_relay.device_context"):
        enabled_harness.deliver_cloud(DEVICE_A.client_id, _service_sub_topic(), payload)

    assert "cloud-user" not in caplog.text
    assert "mqtt-password" not in caplog.text
    assert "p2p-secret" not in caplog.text
    assert "<redacted>" in caplog.text


def test_enabled_logging_ignores_non_service_cloud_topics(
    enabled_harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """Only the diagnostic target `/device/service/sub` emits INFO payload logs."""
    payload = json.dumps({"cmd": "NTP_SYNC", "msgId": "ntp"}).encode()
    ntp_topic = _service_sub_topic().replace("/service/sub", "/ntp/sub")

    with caplog.at_level(logging.INFO, logger="petlibro_relay.device_context"):
        enabled_harness.deliver_cloud(DEVICE_A.client_id, ntp_topic, payload)

    assert "UPSTREAM SERVICE RX" not in caplog.text
