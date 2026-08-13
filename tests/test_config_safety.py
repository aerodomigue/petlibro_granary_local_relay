"""Tests for startup validation that prevents an upstream MQTT loop."""

from __future__ import annotations

import pytest

from conftest import RelayConfigFactory

from petlibro_relay.config import UNSAFE_UPSTREAM_CONFIGURATION_MESSAGE


@pytest.mark.parametrize("upstream_host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_upstream_on_capture_port_is_rejected(
    make_config: RelayConfigFactory, upstream_host: str
) -> None:
    """Literal loopback on the capture proxy port must fail before startup."""
    config = make_config(upstream_host=upstream_host, upstream_port=1883)

    with pytest.raises(ValueError, match="would connect to the local capture proxy"):
        config.validate_upstream_safety()


def test_loopback_upstream_on_unrelated_port_is_allowed(make_config: RelayConfigFactory) -> None:
    """A deliberately unused local port remains valid for outage testing."""
    config = make_config(upstream_host="127.0.0.1", upstream_port=65534)

    config.validate_upstream_safety()


def test_real_cloud_hostname_is_allowed_without_dns_lookup(make_config: RelayConfigFactory) -> None:
    """The guard is literal-only and must not depend on external DNS."""
    config = make_config(upstream_host="mqtt.us.petlibro.com", upstream_port=1883)

    config.validate_upstream_safety()


def test_error_message_is_actionable(make_config: RelayConfigFactory) -> None:
    """Operators get one stable fatal message when the guard rejects config."""
    config = make_config(upstream_host="localhost", upstream_port=1883)

    with pytest.raises(ValueError) as error:
        config.validate_upstream_safety()

    assert str(error.value) == UNSAFE_UPSTREAM_CONFIGURATION_MESSAGE
