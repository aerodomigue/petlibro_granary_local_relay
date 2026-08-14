"""Tests for relay-to-camera-bridge persistent registry reconciliation."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

from petlibro_relay.camera import (
    CameraBridgeMapping,
    CameraBridgeReconciler,
    CameraBridgeRegistrar,
)
from petlibro_relay.config import CameraBridgeSettings
from petlibro_relay.state_shadow import StateShadow

DEVICE_A = "AF03040302A2B5B2CD60"
DEVICE_B = "AF03040302A2B5B2CD61"
UID_A = "PLAF2030000000000001"
UID_B = "PLAF2030000000000002"
IP_A = "10.3.100.90"
IP_A_UPDATED = "10.3.100.91"
IP_B = "10.3.100.92"
WORKER_WAIT_SECONDS = 1.0


class FakeReconciliationClient:
    """In-memory bridge API with a deliberately restartable registry."""

    def __init__(self, online: bool = True) -> None:
        """Create an empty fake camera-bridge runtime registry."""
        self.online = online
        self.registry: dict[str, str | None] = {}
        self.calls: list[CameraBridgeMapping] = []
        self.health_calls = 0
        self._lock = threading.Lock()

    def health(self) -> bool:
        """Return the configured reachability state."""
        self.health_calls += 1
        return self.online

    def registrations(self) -> dict[str, str | None] | None:
        """Return the non-sensitive runtime registry only while online."""
        with self._lock:
            return dict(self.registry) if self.online else None

    def register(self, device_id: str, uid: str, feeder_ip: str | None) -> bool:
        """Record an idempotent bridge PUT without retaining a loggable UID."""
        with self._lock:
            if not self.online:
                return False
            self.calls.append((device_id, uid, feeder_ip))
            self.registry[device_id] = feeder_ip
        return True


def _wait_for_calls(client: FakeReconciliationClient, expected: int) -> None:
    """Wait for asynchronous registration work to reach the fake bridge."""
    deadline = time.monotonic() + WORKER_WAIT_SECONDS
    while time.monotonic() < deadline:
        with client._lock:
            if len(client.calls) >= expected:
                return
        time.sleep(0.01)
    raise AssertionError(f"expected {expected} bridge registrations, got {len(client.calls)}")


def _make_reconciler(
    client: FakeReconciliationClient, mappings: list[CameraBridgeMapping]
) -> tuple[CameraBridgeRegistrar, CameraBridgeReconciler]:
    """Build a reconciler with a mutable persisted mapping source."""
    settings = CameraBridgeSettings(enabled=True, reconcile_interval_seconds=1.0)
    registrar = CameraBridgeRegistrar(settings, client)
    reconciler = CameraBridgeReconciler(settings, client, registrar, lambda: list(mappings))
    return registrar, reconciler


def test_startup_online_registers_persisted_mapping(caplog: pytest.LogCaptureFixture) -> None:
    """An online empty bridge converges from the relay's persisted source."""
    client = FakeReconciliationClient()
    registrar, reconciler = _make_reconciler(client, [(DEVICE_A, UID_A, IP_A)])
    try:
        reconciler.reconcile_once()
        _wait_for_calls(client, 1)

        assert client.registry == {DEVICE_A: IP_A}
        assert UID_A not in caplog.text
    finally:
        reconciler.close()
        registrar.close()


def test_startup_offline_then_online_recovers_persisted_mapping(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unavailable bridge does not block startup and recovers on a later pass."""
    client = FakeReconciliationClient(online=False)
    registrar, reconciler = _make_reconciler(client, [(DEVICE_A, UID_A, IP_A)])
    try:
        with caplog.at_level(logging.INFO, logger="petlibro_relay.camera"):
            reconciler.reconcile_once()
            assert client.calls == []

            client.online = True
            reconciler.reconcile_once()
            _wait_for_calls(client, 1)

        assert client.registry == {DEVICE_A: IP_A}
        assert client.health_calls == 2
        assert "CAMERA BRIDGE OFFLINE" in caplog.text
        assert "CAMERA BRIDGE RECONCILE FAILED reason=unreachable" in caplog.text
        assert "CAMERA BRIDGE ONLINE" in caplog.text
        assert f"CAMERA BRIDGE RECONCILE device={DEVICE_A} action=register" in caplog.text
        assert f"CAMERA BRIDGE REGISTERED device={DEVICE_A}" in caplog.text
    finally:
        reconciler.close()
        registrar.close()


def test_bridge_restart_re_registers_existing_persisted_mapping() -> None:
    """A cleared in-memory bridge registry is restored without a feeder reconnect."""
    client = FakeReconciliationClient()
    registrar, reconciler = _make_reconciler(client, [(DEVICE_A, UID_A, IP_A)])
    try:
        reconciler.reconcile_once()
        _wait_for_calls(client, 1)

        client.registry.clear()
        reconciler.reconcile_once()
        _wait_for_calls(client, 2)

        assert client.registry == {DEVICE_A: IP_A}
    finally:
        reconciler.close()
        registrar.close()


def test_duplicate_event_registration_does_not_cause_reconcile_duplicate() -> None:
    """Matching runtime state keeps duplicate event-driven registration harmless."""
    client = FakeReconciliationClient()
    registrar, reconciler = _make_reconciler(client, [(DEVICE_A, UID_A, IP_A)])
    try:
        assert registrar.register(DEVICE_A, UID_A, IP_A) is True
        assert registrar.register(DEVICE_A, UID_A, IP_A) is False
        _wait_for_calls(client, 1)

        reconciler.reconcile_once()
        time.sleep(0.05)

        assert client.calls == [(DEVICE_A, UID_A, IP_A)]
    finally:
        reconciler.close()
        registrar.close()


def test_reconcile_updates_bridge_when_persisted_ip_changes() -> None:
    """A persisted feeder IP change becomes one bridge update, not a duplicate loop."""
    mappings: list[CameraBridgeMapping] = [(DEVICE_A, UID_A, IP_A)]
    client = FakeReconciliationClient()
    registrar, reconciler = _make_reconciler(client, mappings)
    try:
        reconciler.reconcile_once()
        _wait_for_calls(client, 1)

        mappings[:] = [(DEVICE_A, UID_A, IP_A_UPDATED)]
        reconciler.reconcile_once()
        _wait_for_calls(client, 2)

        assert client.calls == [(DEVICE_A, UID_A, IP_A), (DEVICE_A, UID_A, IP_A_UPDATED)]
        assert client.registry == {DEVICE_A: IP_A_UPDATED}
    finally:
        reconciler.close()
        registrar.close()


def test_reconcile_registers_multiple_persisted_devices() -> None:
    """Every persisted device converges independently into a shared bridge."""
    client = FakeReconciliationClient()
    registrar, reconciler = _make_reconciler(
        client,
        [(DEVICE_A, UID_A, IP_A), (DEVICE_B, UID_B, IP_B)],
    )
    try:
        reconciler.reconcile_once()
        _wait_for_calls(client, 2)

        assert client.registry == {DEVICE_A: IP_A, DEVICE_B: IP_B}
    finally:
        reconciler.close()
        registrar.close()


def test_relay_restart_reloads_persisted_camera_uid_mapping(tmp_path: Path) -> None:
    """A new relay process restores registrations from SQLite without a device event."""
    database_path = tmp_path / "state_shadow.sqlite3"
    initial_shadow = StateShadow(str(database_path))
    try:
        assert initial_shadow.record_camera_uid(DEVICE_A, UID_A, IP_A) is True
    finally:
        initial_shadow.close()

    restarted_shadow = StateShadow(str(database_path))
    client = FakeReconciliationClient()
    settings = CameraBridgeSettings(enabled=True, reconcile_interval_seconds=1.0)
    registrar = CameraBridgeRegistrar(settings, client)
    reconciler = CameraBridgeReconciler(
        settings,
        client,
        registrar,
        lambda: [
            (mapping.device_id, mapping.uid, mapping.feeder_ip)
            for mapping in restarted_shadow.get_camera_uids()
        ],
    )
    try:
        reconciler.reconcile_once()
        _wait_for_calls(client, 1)

        assert client.registry == {DEVICE_A: IP_A}
    finally:
        reconciler.close()
        registrar.close()
        restarted_shadow.close()


def test_reconciler_worker_stops_cleanly() -> None:
    """The optional reconciliation thread is joined during relay shutdown."""
    client = FakeReconciliationClient(online=False)
    registrar, reconciler = _make_reconciler(client, [])
    reconciler.start()
    try:
        thread = reconciler._thread
        assert thread is not None
        assert thread.is_alive()
    finally:
        reconciler.close()
        registrar.close()

    assert not thread.is_alive()
