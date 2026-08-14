"""Coverage for opt-in device-start event diagnostics."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from conftest import RelayConfigFactory
from test_multi_device_isolation import DEVICE_A, TOPIC_A, Harness, build_harness


@pytest.fixture
def disabled_harness(make_config: RelayConfigFactory) -> Iterator[Harness]:
    """Provide a bridge with the diagnostic at its safe default."""
    yield from build_harness(make_config, (DEVICE_A,))


@pytest.fixture
def enabled_harness(make_config: RelayConfigFactory) -> Iterator[Harness]:
    """Provide a bridge with device-start diagnostics enabled explicitly."""
    yield from build_harness(make_config, (DEVICE_A,), log_device_start_event=True)


def test_device_start_event_diagnostic_is_quiet_by_default(
    disabled_harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """The normal feeder boot path gains no detailed INFO log by default."""
    payload = json.dumps({"cmd": "DEVICE_START_EVENT", "uuid": "device-uuid"}).encode()

    with caplog.at_level(logging.INFO, logger="petlibro_relay.mqtt_bridge"):
        disabled_harness.deliver_local(TOPIC_A, payload)

    assert "DEVICE START EVENT RX" not in caplog.text
    assert disabled_harness.pending(DEVICE_A.client_id) == 1


def test_enabled_device_start_event_log_includes_uuid_and_safe_payload(
    enabled_harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """The opt-in diagnostic retains useful identity fields and redacts secrets."""
    payload = json.dumps(
        {
            "cmd": "DEVICE_START_EVENT",
            "uuid": "device-uuid",
            "firmware": "V3.0.30",
            "password": "must-not-appear",
        }
    ).encode()

    with caplog.at_level(logging.INFO, logger="petlibro_relay.mqtt_bridge"):
        enabled_harness.deliver_local(TOPIC_A, payload)

    assert "DEVICE START EVENT RX source=device" in caplog.text
    assert "cmd=DEVICE_START_EVENT" in caplog.text
    assert "uuid=device-uuid" in caplog.text
    assert '"uuid":"device-uuid"' in caplog.text
    assert "must-not-appear" not in caplog.text
    assert "<redacted>" in caplog.text
    assert enabled_harness.pending(DEVICE_A.client_id) == 1


def test_enabled_diagnostic_ignores_other_device_events(
    enabled_harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """Only DEVICE_START_EVENT gets the extra detailed record."""
    payload = json.dumps({"cmd": "GRAIN_OUTPUT_EVENT", "uuid": "device-uuid"}).encode()

    with caplog.at_level(logging.INFO, logger="petlibro_relay.mqtt_bridge"):
        enabled_harness.deliver_local(TOPIC_A, payload)

    assert "DEVICE START EVENT RX" not in caplog.text
    assert enabled_harness.pending(DEVICE_A.client_id) == 1
