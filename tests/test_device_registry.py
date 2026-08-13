"""Tests for multi-device enrollment in `DeviceRegistry`.

Two properties matter here. Devices must accumulate rather than displace each
other - the whole point of the multi-device model - and a device on the LAN
must not be able to get itself bridged when the operator has said newly seen
devices need approval.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from petlibro_relay.device_registry import (
    LEGACY_ACTIVE_CLIENT_ID_KEY,
    SECONDS_PER_HOUR,
    DeviceIdentity,
    DeviceRegistry,
    DeviceStatus,
    RecordOutcome,
)

RETENTION_SECONDS = 72 * SECONDS_PER_HOUR

DEVICE_A = DeviceIdentity(client_id="DEVICE-A", username="user-a", password="pass-a")
DEVICE_B = DeviceIdentity(client_id="DEVICE-B", username="user-b", password="pass-b")
DEVICE_C = DeviceIdentity(client_id="DEVICE-C", username="user-c", password="pass-c")


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Path to a fresh registry database."""
    return str(tmp_path / "registry.sqlite3")


@pytest.fixture
def registry(db_path: str) -> Iterator[DeviceRegistry]:
    """A registry with the production retention window and auto-enrollment on."""
    instance = DeviceRegistry(db_path, retention_seconds=RETENTION_SECONDS)
    yield instance
    instance.close()


def age_device(db_path: str, client_id: str, hours: float) -> None:
    """Backdate a device's `last_seen_at` so it looks like it went quiet."""
    connection = sqlite3.connect(db_path)
    with connection:
        connection.execute(
            "UPDATE device_identities SET last_seen_at = ? WHERE client_id = ?",
            (time.time() - hours * SECONDS_PER_HOUR, client_id),
        )
    connection.close()


def bridged_ids(registry: DeviceRegistry) -> list[str]:
    """Client ids the relay would open an upstream session for."""
    return sorted(identity.client_id for identity in registry.get_bridgeable())


def test_devices_accumulate_instead_of_displacing_each_other(registry: DeviceRegistry) -> None:
    """Three feeders connecting leaves three bridgeable devices, not one."""
    assert registry.get_bridgeable() == []

    assert registry.record(DEVICE_A) is RecordOutcome.ENROLLED
    assert registry.record(DEVICE_B) is RecordOutcome.ENROLLED
    assert registry.record(DEVICE_C) is RecordOutcome.ENROLLED

    assert bridged_ids(registry) == ["DEVICE-A", "DEVICE-B", "DEVICE-C"]


def test_reconnect_refreshes_credentials_without_duplicating(
    registry: DeviceRegistry, db_path: str
) -> None:
    """A device reconnecting updates its row rather than adding another."""
    registry.record(DEVICE_A)
    age_device(db_path, "DEVICE-A", hours=10)

    rotated = DeviceIdentity(client_id="DEVICE-A", username="user-a", password="rotated-secret")
    assert registry.record(rotated) is RecordOutcome.REFRESHED

    identity = registry.get("DEVICE-A")
    assert identity is not None
    assert identity.password == "rotated-secret", "credentials must be refreshed on reconnect"
    assert bridged_ids(registry) == ["DEVICE-A"]

    connection = sqlite3.connect(db_path)
    (last_seen,) = connection.execute(
        "SELECT last_seen_at FROM device_identities WHERE client_id = 'DEVICE-A'"
    ).fetchone()
    connection.close()
    assert time.time() - last_seen < 60, "last_seen_at must be refreshed"


def test_auto_enroll_off_parks_new_devices_as_candidates(db_path: str) -> None:
    """With approval required, a new device is stored but never bridged."""
    registry = DeviceRegistry(db_path, retention_seconds=RETENTION_SECONDS, auto_enroll=False)
    try:
        assert registry.record(DEVICE_A) is RecordOutcome.PENDING_APPROVAL

        assert registry.get_bridgeable() == [], "an unapproved device must not be bridged"
        entries = registry.entries()
        assert [entry.status for entry in entries] == [DeviceStatus.CANDIDATE]
        assert entries[0].bridged is False
    finally:
        registry.close()


def test_candidate_stays_a_candidate_when_it_reconnects(db_path: str) -> None:
    """Reconnecting must not be a way to talk yourself into being enrolled."""
    registry = DeviceRegistry(db_path, retention_seconds=RETENTION_SECONDS, auto_enroll=False)
    try:
        registry.record(DEVICE_A)
        assert registry.record(DEVICE_A) is RecordOutcome.PENDING_APPROVAL
        assert registry.get_bridgeable() == []
    finally:
        registry.close()


def test_enrollment_survives_restart_for_every_device(db_path: str) -> None:
    """Restarting rebuilds all three devices, not just the most recent one."""
    first = DeviceRegistry(db_path, retention_seconds=RETENTION_SECONDS)
    first.record(DEVICE_A)
    first.record(DEVICE_B)
    first.record(DEVICE_C)
    first.close()

    reopened = DeviceRegistry(db_path, retention_seconds=RETENTION_SECONDS)
    restored = bridged_ids(reopened)
    reopened.close()

    assert restored == ["DEVICE-A", "DEVICE-B", "DEVICE-C"]


def test_expired_device_stops_being_bridgeable(registry: DeviceRegistry, db_path: str) -> None:
    """A device quiet past the TTL drops out without affecting the others."""
    registry.record(DEVICE_A)
    registry.record(DEVICE_B)
    age_device(db_path, "DEVICE-A", hours=73)

    assert bridged_ids(registry) == ["DEVICE-B"]
    assert [entry.client_id for entry in registry.entries()] == ["DEVICE-B"]


def test_purge_removes_only_expired_devices(registry: DeviceRegistry, db_path: str) -> None:
    """Purging is selective: a live device is never collected with a stale one."""
    registry.record(DEVICE_A)
    registry.record(DEVICE_B)
    age_device(db_path, "DEVICE-A", hours=100)

    assert registry.purge_expired() == 1
    assert bridged_ids(registry) == ["DEVICE-B"]


def test_migration_preserves_the_previous_active_and_candidate_decision(db_path: str) -> None:
    """A legacy database keeps bridging exactly the device it used to bridge.

    Auto-enrollment is on here: the point is that it must not retroactively
    promote a device the single-active model deliberately refused.
    """
    legacy = sqlite3.connect(db_path)
    with legacy:
        legacy.execute(
            """
            CREATE TABLE device_identities (
                client_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                first_seen_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),
                last_seen_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
            )
            """
        )
        legacy.execute("CREATE TABLE registry_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for device in (DEVICE_A, DEVICE_B):
            legacy.execute(
                "INSERT INTO device_identities (client_id, username, password) VALUES (?, ?, ?)",
                (device.client_id, device.username, device.password),
            )
        legacy.execute(
            "INSERT INTO registry_state (key, value) VALUES (?, ?)",
            (LEGACY_ACTIVE_CLIENT_ID_KEY, DEVICE_A.client_id),
        )
    legacy.close()

    migrated = DeviceRegistry(db_path, retention_seconds=RETENTION_SECONDS)
    try:
        assert bridged_ids(migrated) == ["DEVICE-A"], "the previously active device stays bridged"
        statuses = {entry.client_id: entry.status for entry in migrated.entries()}
        assert statuses == {
            "DEVICE-A": DeviceStatus.KNOWN,
            "DEVICE-B": DeviceStatus.CANDIDATE,
        }
        identity = migrated.get("DEVICE-A")
        assert identity is not None
        assert identity.password == DEVICE_A.password, "credentials must survive migration"
    finally:
        migrated.close()
