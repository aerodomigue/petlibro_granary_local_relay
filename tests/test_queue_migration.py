"""Upgrading a mono-device deployment must not lose or misroute anything.

The pre-multi-device queue had no device column. Its rows still name their
device in the topic, so the migration recovers that rather than guessing -
and anything it genuinely cannot resolve is kept and reported instead of
being quietly dropped.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import RelayConfigFactory

from petlibro_relay.device_context import LOCAL_TO_UPSTREAM, UPSTREAM_TO_LOCAL
from petlibro_relay.device_registry import DeviceIdentity, DeviceRegistry
from petlibro_relay.message_queue import MessageQueue
from petlibro_relay.state_shadow import StateShadow

DEVICE_ID = "TESTDEVICE0000000001"
DEVICE_TOPIC = f"dl/PLAF203/{DEVICE_ID}/device/event/post"
LEGACY_SCHEMA_SQL = """
CREATE TABLE pending_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,
    topic TEXT NOT NULL,
    payload BLOB NOT NULL,
    qos INTEGER NOT NULL,
    coalesce_key TEXT,
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
)
"""


def write_legacy_queue(db_path: str, rows: list[tuple[str, str]]) -> None:
    """Create a queue database in the pre-multi-device shape."""
    connection = sqlite3.connect(db_path)
    with connection:
        connection.execute(LEGACY_SCHEMA_SQL)
        connection.executemany(
            "INSERT INTO pending_messages (direction, topic, payload, qos) VALUES (?, ?, ?, 0)",
            [(direction, topic, b'{"cmd":"HEARTBEAT"}') for direction, topic in rows],
        )
    connection.close()


@pytest.fixture
def legacy_queue_path(tmp_path: Path) -> str:
    """Path to a legacy queue holding one message in each direction."""
    db_path = str(tmp_path / "relay_queue.sqlite3")
    write_legacy_queue(
        db_path,
        [
            (LOCAL_TO_UPSTREAM, DEVICE_TOPIC),
            (LOCAL_TO_UPSTREAM, DEVICE_TOPIC),
            (UPSTREAM_TO_LOCAL, f"dl/PLAF203/{DEVICE_ID}/device/service/sub"),
        ],
    )
    return db_path


def test_backlog_is_tagged_with_the_device_from_its_topic(legacy_queue_path: str) -> None:
    """A mono-device backlog survives the upgrade and stays routable."""
    queue = MessageQueue(legacy_queue_path, max_size_per_direction=100)
    try:
        assert queue.count(DEVICE_ID, LOCAL_TO_UPSTREAM) == 2
        assert queue.count(DEVICE_ID, UPSTREAM_TO_LOCAL) == 1
        assert queue.depth_by_device() == {DEVICE_ID: 3}
        assert queue.unroutable_count() == 0
    finally:
        queue.close()


def test_migrated_messages_keep_their_payload_and_order(legacy_queue_path: str) -> None:
    """Migration is metadata-only: content and FIFO order are untouched."""
    queue = MessageQueue(legacy_queue_path, max_size_per_direction=100)
    try:
        oldest = queue.peek_oldest(DEVICE_ID, LOCAL_TO_UPSTREAM)

        assert oldest is not None
        assert oldest.id == 1, "the original insertion order must be preserved"
        assert oldest.payload == b'{"cmd":"HEARTBEAT"}'
        assert oldest.topic == DEVICE_TOPIC
    finally:
        queue.close()


def test_unroutable_legacy_rows_are_kept_not_deleted(tmp_path: Path) -> None:
    """A row whose topic names no device is reported, never silently dropped."""
    db_path = str(tmp_path / "queue.sqlite3")
    write_legacy_queue(
        db_path, [(LOCAL_TO_UPSTREAM, DEVICE_TOPIC), (LOCAL_TO_UPSTREAM, "totally/unexpected")]
    )

    queue = MessageQueue(db_path, max_size_per_direction=100)
    try:
        assert queue.unroutable_count() == 1
        assert queue.count(DEVICE_ID, LOCAL_TO_UPSTREAM) == 1

        connection = sqlite3.connect(db_path)
        (total,) = connection.execute("SELECT COUNT(*) FROM pending_messages").fetchone()
        connection.close()
        assert total == 2, "nothing may be deleted by the migration"
    finally:
        queue.close()


def test_migration_is_idempotent(legacy_queue_path: str) -> None:
    """Reopening an already-migrated database changes nothing."""
    first = MessageQueue(legacy_queue_path, max_size_per_direction=100)
    first.close()

    second = MessageQueue(legacy_queue_path, max_size_per_direction=100)
    try:
        assert second.depth_by_device() == {DEVICE_ID: 3}
    finally:
        second.close()


def test_full_mono_device_deployment_upgrades_intact(
    make_config: RelayConfigFactory, tmp_path: Path
) -> None:
    """Identity, queue and shadow all survive an upgrade of a real deployment."""
    config = make_config(queue_db_path=str(tmp_path / "relay_queue.sqlite3"))
    write_legacy_queue(config.queue_db_path, [(LOCAL_TO_UPSTREAM, DEVICE_TOPIC)])

    legacy_registry = sqlite3.connect(config.device_registry_db_path)
    with legacy_registry:
        legacy_registry.execute(
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
        legacy_registry.execute("CREATE TABLE registry_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        legacy_registry.execute(
            "INSERT INTO device_identities (client_id, username, password) VALUES (?, ?, ?)",
            (DEVICE_ID, "legacy-user", "legacy-pass"),
        )
        legacy_registry.execute(
            "INSERT INTO registry_state (key, value) VALUES ('active_client_id', ?)", (DEVICE_ID,)
        )
    legacy_registry.close()

    shadow = StateShadow(config.state_shadow_db_path)
    shadow.update_desired(DEVICE_ID, {"grainNum": 4})
    shadow.close()

    registry = DeviceRegistry(config.device_registry_db_path)
    queue = MessageQueue(config.queue_db_path, config.max_queue_size)
    reopened_shadow = StateShadow(config.state_shadow_db_path)
    try:
        assert registry.get_bridgeable() == [
            DeviceIdentity(DEVICE_ID, "legacy-user", "legacy-pass", "PLAF203")
        ]
        assert queue.count(DEVICE_ID, LOCAL_TO_UPSTREAM) == 1
        assert reopened_shadow.get_desired(DEVICE_ID) == {"grainNum": 4}
    finally:
        registry.close()
        queue.close()
        reopened_shadow.close()
