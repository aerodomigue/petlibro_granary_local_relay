"""Tests for transition-aware upstream MQTT observability."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import cast

import pytest

from petlibro_relay.mqtt_bridge import MqttBridge
from petlibro_relay.observability.telemetry import (
    OFFLINE_SUMMARY_INTERVAL_SECONDS,
    RelayTelemetry,
    UpstreamTransitionKind,
)


@dataclass
class ManualClock:
    """Controllable clock for deterministic outage durations and rate limits."""

    now: float = 0.0

    def __call__(self) -> float:
        """Return the current synthetic epoch."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move time forward by a non-negative interval."""
        self.now += seconds


def test_online_disconnect_is_one_session_loss_and_warning(caplog: pytest.LogCaptureFixture) -> None:
    """ONLINE -> disconnect is a true lost session, not a failed reconnect."""
    clock = ManualClock()
    telemetry = RelayTelemetry(clock=clock)
    telemetry.upstream_connect_attempt()
    assert telemetry.upstream_online().kind is UpstreamTransitionKind.ONLINE
    clock.advance(42)

    transition = telemetry.upstream_disconnected("Unspecified error", "server=false")
    with caplog.at_level(logging.DEBUG):
        MqttBridge._log_upstream_transition(cast(MqttBridge, None), transition)

    counters = telemetry.snapshot()["upstream"]["counters"]
    assert transition.kind is UpstreamTransitionKind.SESSION_LOST
    assert transition.state_before == "ONLINE"
    assert transition.session_duration_seconds == 42
    assert counters["sessions_lost"] == 1
    assert counters.get("reconnect_failures", 0) == 0
    assert "UPSTREAM lost reason=Unspecified error session_duration=42.0s state_before=ONLINE" in caplog.text


def test_connecting_disconnect_is_retry_failure_not_session_loss(caplog: pytest.LogCaptureFixture) -> None:
    """MQTT_CONNECTING -> disconnect is a failed CONNECT/CONNACK attempt only."""
    telemetry = RelayTelemetry(clock=ManualClock())
    telemetry.upstream_connect_attempt()

    transition = telemetry.upstream_disconnected("Unspecified error")
    with caplog.at_level(logging.DEBUG):
        MqttBridge._log_upstream_transition(cast(MqttBridge, None), transition)

    counters = telemetry.snapshot()["upstream"]["counters"]
    assert transition.kind is UpstreamTransitionKind.RETRY_FAILED
    assert transition.state_before == "MQTT_CONNECTING"
    assert counters.get("sessions_lost", 0) == 0
    assert counters["reconnect_failures"] == 1
    assert counters["connack_timeouts"] == 1
    assert "Upstream reconnect failed attempt=1 reason=Unspecified error state_before=MQTT_CONNECTING" in caplog.text
    assert "UPSTREAM lost" not in caplog.text


def test_retries_during_one_outage_do_not_create_extra_session_losses() -> None:
    """Repeated reconnect callbacks are failures in one outage, never new sessions lost."""
    clock = ManualClock()
    telemetry = RelayTelemetry(clock=clock)
    telemetry.upstream_connect_attempt()
    telemetry.upstream_online()
    telemetry.upstream_disconnected("socket reset")

    summaries = []
    for _ in range(10):
        telemetry.upstream_connect_attempt()
        summaries.append(telemetry.upstream_disconnected("Unspecified error").offline_summary_due)
        clock.advance(60)

    counters = telemetry.snapshot()["upstream"]["counters"]
    assert counters["sessions_lost"] == 1
    assert counters["reconnect_failures"] == 10
    assert summaries.count(True) == 1


def test_restored_reports_outage_duration_and_resets_outage_attempts() -> None:
    """The next CONNACK reports one outage summary then clears outage-local state."""
    clock = ManualClock()
    telemetry = RelayTelemetry(clock=clock)
    telemetry.upstream_connect_attempt()
    clock.advance(30)
    telemetry.upstream_disconnected("Unspecified error")
    clock.advance(30)
    telemetry.upstream_connect_attempt()

    transition = telemetry.upstream_online()
    outage = telemetry.snapshot()["upstream"]["outage"]

    assert transition.kind is UpstreamTransitionKind.RESTORED
    assert transition.downtime_seconds == 60
    assert transition.failed_attempts == 1
    assert outage["attempts"] == 0
    assert outage["failed_attempts"] == 0
    assert outage["downtime_seconds"] is None


def test_tcp_failures_and_connack_refusals_are_distinct() -> None:
    """Paho TCP failures and broker refusals retain separate dashboard counters."""
    telemetry = RelayTelemetry(clock=ManualClock())
    telemetry.upstream_connect_attempt()
    tcp_transition = telemetry.upstream_connect_failed()
    telemetry.upstream_connect_attempt()
    refused_transition = telemetry.upstream_refused("Not authorized")
    counters = telemetry.snapshot()["upstream"]["counters"]

    assert tcp_transition.kind is UpstreamTransitionKind.TCP_CONNECT_FAILED
    assert refused_transition.kind is UpstreamTransitionKind.CONNACK_REFUSED
    assert counters["tcp_connect_failures"] == 1
    assert counters["connack_refused"] == 1
    assert counters["reconnect_failures"] == 2


def test_long_outage_warning_is_rate_limited(caplog: pytest.LogCaptureFixture) -> None:
    """The five-minute offline summary is emitted at most once per interval."""
    clock = ManualClock()
    telemetry = RelayTelemetry(clock=clock)
    telemetry.upstream_connect_attempt()

    with caplog.at_level(logging.DEBUG):
        first = telemetry.upstream_disconnected("Unspecified error")
        MqttBridge._log_upstream_transition(cast(MqttBridge, None), first)
        clock.advance(OFFLINE_SUMMARY_INTERVAL_SECONDS - 1)
        telemetry.upstream_connect_attempt()
        second = telemetry.upstream_disconnected("Unspecified error")
        MqttBridge._log_upstream_transition(cast(MqttBridge, None), second)
        clock.advance(1)
        telemetry.upstream_connect_attempt()
        third = telemetry.upstream_disconnected("Unspecified error")
        MqttBridge._log_upstream_transition(cast(MqttBridge, None), third)
        clock.advance(1)
        telemetry.upstream_connect_attempt()
        fourth = telemetry.upstream_disconnected("Unspecified error")
        MqttBridge._log_upstream_transition(cast(MqttBridge, None), fourth)

    assert first.offline_summary_due is False
    assert second.offline_summary_due is False
    assert third.offline_summary_due is True
    assert fourth.offline_summary_due is False
    assert caplog.text.count("UPSTREAM still offline") == 1
