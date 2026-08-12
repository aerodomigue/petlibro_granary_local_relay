"""Tests for dashboard-only upstream connection counters."""

from __future__ import annotations

from petlibro_relay.observability.telemetry import RelayTelemetry


def test_session_loss_is_not_counted_as_reconnect_failure() -> None:
    """Only ONLINE -> disconnected counts as a lost MQTT session."""
    telemetry = RelayTelemetry(started_at=1.0)

    telemetry.upstream_connect_attempt()
    telemetry.upstream_online()
    telemetry.upstream_disconnected("Unspecified error")
    telemetry.upstream_connect_attempt()
    telemetry.upstream_disconnected("Unspecified error")
    counters = telemetry.snapshot()["upstream"]["counters"]

    assert counters["sessions_lost"] == 1
    assert counters["connack_timeouts"] == 1
    assert counters["reconnect_failures"] == 1
