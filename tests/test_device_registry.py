"""Tests for the sticky active-device logic in `DeviceRegistry`.

The point of these is that a device on the LAN must not be able to take over
the relay just by connecting more recently than the real feeder.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from petlibro_relay.device_registry import (
    ACTIVE_CLIENT_ID_KEY,
    SECONDS_PER_HOUR,
    DeviceIdentity,
    DeviceRegistry,
    RecordOutcome,
)

RETENTION_SECONDS = 72 * SECONDS_PER_HOUR

DEVICE_A = DeviceIdentity(client_id="DEVICE-A", username="user-a", password="pass-a")
DEVICE_B = DeviceIdentity(client_id="DEVICE-B", username="user-b", password="pass-b")


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Path to a fresh registry database."""
    return str(tmp_path / "registry.sqlite3")


@pytest.fixture
def registry(db_path: str) -> Iterator[DeviceRegistry]:
    """A registry with the production retention window."""
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


def test_first_connect_becomes_active(registry: DeviceRegistry) -> None:
    """Test 1: an empty registry adopts the first device that connects."""
    assert registry.get_active() is None

    assert registry.record(DEVICE_A) is RecordOutcome.PROMOTED_TO_ACTIVE

    active = registry.get_active()
    assert active is not None
    assert active.client_id == "DEVICE-A"


def test_same_device_reconnecting_stays_active_and_refreshes(
    registry: DeviceRegistry, db_path: str
) -> None:
    """Test 2: the active device keeps the role, and its credentials are updated."""
    registry.record(DEVICE_A)
    age_device(db_path, "DEVICE-A", hours=10)

    rotated = DeviceIdentity(client_id="DEVICE-A", username="user-a", password="rotated-secret")
    assert registry.record(rotated) is RecordOutcome.REFRESHED_ACTIVE

    active = registry.get_active()
    assert active is not None
    assert active.client_id == "DEVICE-A"
    assert active.password == "rotated-secret", "credentials must be refreshed on reconnect"

    connection = sqlite3.connect(db_path)
    (last_seen,) = connection.execute(
        "SELECT last_seen_at FROM device_identities WHERE client_id = 'DEVICE-A'"
    ).fetchone()
    connection.close()
    assert time.time() - last_seen < 60, "last_seen_at must be refreshed"


def test_foreign_device_becomes_candidate_not_active(registry: DeviceRegistry) -> None:
    """Test 3: a different device connecting does not displace the active one."""
    registry.record(DEVICE_A)

    assert registry.record(DEVICE_B) is RecordOutcome.STORED_AS_CANDIDATE

    active = registry.get_active()
    assert active is not None
    assert active.client_id == "DEVICE-A", "a foreign device must never take over automatically"
    assert [c.client_id for c in registry.get_candidates()] == ["DEVICE-B"]


def test_active_survives_restart_even_with_newer_candidate(db_path: str) -> None:
    """Test 4: the active role is sticky across a restart, not "most recently seen"."""
    first = DeviceRegistry(db_path, retention_seconds=RETENTION_SECONDS)
    first.record(DEVICE_A)
    first.record(DEVICE_B)  # B is now the most recently seen identity
    first.close()

    reopened = DeviceRegistry(db_path, retention_seconds=RETENTION_SECONDS)
    active = reopened.get_active()
    reopened.close()

    assert active is not None
    assert active.client_id == "DEVICE-A", "restart must not promote the newer candidate"


def test_expired_active_frees_the_role(registry: DeviceRegistry, db_path: str) -> None:
    """Test 5: once the active device has been quiet past the TTL, the next one takes over."""
    registry.record(DEVICE_A)
    age_device(db_path, "DEVICE-A", hours=73)

    assert registry.get_active() is None, "an expired active identity must not be returned"

    assert registry.record(DEVICE_B) is RecordOutcome.PROMOTED_TO_ACTIVE
    active = registry.get_active()
    assert active is not None
    assert active.client_id == "DEVICE-B"


def test_purge_removes_expired_and_vacates_the_role(registry: DeviceRegistry, db_path: str) -> None:
    """Expired identities are forgotten and a stale active pointer is cleared."""
    registry.record(DEVICE_A)
    registry.record(DEVICE_B)
    age_device(db_path, "DEVICE-A", hours=100)
    age_device(db_path, "DEVICE-B", hours=100)

    assert registry.purge_expired() == 2

    connection = sqlite3.connect(db_path)
    remaining = connection.execute("SELECT COUNT(*) FROM device_identities").fetchone()[0]
    pointer = connection.execute(
        "SELECT value FROM registry_state WHERE key = ?", (ACTIVE_CLIENT_ID_KEY,)
    ).fetchone()
    connection.close()

    assert remaining == 0
    assert pointer is None, "the active pointer must not outlive the identity it referred to"


def test_migration_adopts_identity_learned_before_the_state_table(db_path: str) -> None:
    """A database from an earlier version keeps working without waiting for a new CONNECT."""
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
        legacy.execute(
            "INSERT INTO device_identities (client_id, username, password) VALUES (?, ?, ?)",
            (DEVICE_A.client_id, DEVICE_A.username, DEVICE_A.password),
        )
    legacy.close()

    migrated = DeviceRegistry(db_path, retention_seconds=RETENTION_SECONDS)
    active = migrated.get_active()
    migrated.close()

    assert active is not None
    assert active.client_id == "DEVICE-A"
