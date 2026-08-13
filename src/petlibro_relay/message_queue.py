"""Durable, disk-persisted FIFO queue used to bridge MQTT outages.

Every message crossing the relay is written here before delivery is attempted.
A separate pump (see `DeliveryPump`) drains the queue as fast as the destination
broker allows. Because the queue lives in a SQLite file on a mounted volume, it
survives both a lost connection and a full container restart: whatever arrived
while the upstream (or local) broker was offline is still there once it comes
back, and gets replayed in the original order.

Every row is tagged with the device it belongs to. Each device has its own
upstream session with its own outages, so "the backlog" is never global:
draining, counting and replaying are always scoped to one device, and a
device whose cloud session is healthy keeps flowing while another's backlog
waits.
"""

from __future__ import annotations

import logging
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import protocol

_LOGGER = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pending_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,
    topic TEXT NOT NULL,
    payload BLOB NOT NULL,
    qos INTEGER NOT NULL,
    coalesce_key TEXT,
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
)
"""
_CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_pending_messages_direction ON pending_messages (direction, id)"
)
_CREATE_DEVICE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_pending_messages_device ON pending_messages "
    "(device_id, direction, id)"
)
_ADD_COALESCE_KEY_COLUMN_SQL = "ALTER TABLE pending_messages ADD COLUMN coalesce_key TEXT"
_ADD_DEVICE_ID_COLUMN_SQL = "ALTER TABLE pending_messages ADD COLUMN device_id TEXT"
_ADD_PRODUCT_ID_COLUMN_SQL = "ALTER TABLE pending_messages ADD COLUMN product_id TEXT"


@dataclass(frozen=True, slots=True)
class QueuedMessage:
    """A single message pending delivery."""

    id: int
    topic: str
    payload: bytes
    qos: int
    created_at: float


class MessageQueue:
    """SQLite-backed FIFO queue, partitioned by `direction`.

    One `MessageQueue` instance is shared by both relay directions
    (`local-to-upstream` and `upstream-to-local`); each direction is stored as
    its own logical queue via the `direction` column, so a stall in one
    direction never blocks or interleaves with the other.
    """

    def __init__(self, db_path: str, max_size_per_direction: int) -> None:
        """Open (or create) the queue database.

        Args:
            db_path: Filesystem path to the SQLite database file.
            max_size_per_direction: Oldest messages beyond this count are
                dropped (with a warning) so a long outage cannot grow the
                queue without bound.
        """
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._max_size_per_direction = max_size_per_direction
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(db_path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        with self._connection:
            self._connection.execute(_CREATE_TABLE_SQL)
            self._connection.execute(_CREATE_INDEX_SQL)
            self._migrate_schema_locked()
            self._connection.execute(_CREATE_DEVICE_INDEX_SQL)

    def _migrate_schema_locked(self) -> None:
        """Bring a database created by an earlier version up to date.

        The device columns are backfilled from each row's own topic, which
        already names the device it belongs to - so a mono-device backlog
        survives the upgrade and is replayed to the right device rather than
        guessed at. A row whose topic cannot be parsed keeps a NULL
        `device_id`: it is left in place and reported, never deleted, but it
        can no longer be routed and so will not be delivered.
        """
        cursor = self._connection.execute("PRAGMA table_info(pending_messages)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if "coalesce_key" not in existing_columns:
            self._connection.execute(_ADD_COALESCE_KEY_COLUMN_SQL)
            _LOGGER.info("Migrated queue schema: added coalesce_key column")
        if "device_id" in existing_columns and "product_id" in existing_columns:
            return
        if "device_id" not in existing_columns:
            self._connection.execute(_ADD_DEVICE_ID_COLUMN_SQL)
        if "product_id" not in existing_columns:
            self._connection.execute(_ADD_PRODUCT_ID_COLUMN_SQL)
        self._backfill_device_columns_locked()

    def _backfill_device_columns_locked(self) -> None:
        """Derive device/product for pre-multi-device rows from their topics."""
        rows = self._connection.execute(
            "SELECT id, topic FROM pending_messages WHERE device_id IS NULL"
        ).fetchall()
        resolved: list[tuple[str, str, int]] = []
        unroutable = 0
        for message_id, topic in rows:
            address = protocol.parse_topic(str(topic))
            if address is None:
                unroutable += 1
                continue
            resolved.append((address.device_id, address.product_id, int(message_id)))
        if resolved:
            self._connection.executemany(
                "UPDATE pending_messages SET device_id = ?, product_id = ? WHERE id = ?", resolved
            )
        if resolved or unroutable:
            _LOGGER.info(
                "Migrated queue schema: tagged %d pending message(s) with their device",
                len(resolved),
            )
        if unroutable:
            _LOGGER.warning(
                "%d pending message(s) have a topic that carries no device id. They are kept "
                "but cannot be routed to a device, so they will never be delivered.",
                unroutable,
            )

    def enqueue(
        self,
        device_id: str,
        direction: str,
        topic: str,
        payload: bytes,
        qos: int,
        coalesce_key: str | None = None,
        product_id: str = protocol.PRODUCT_ID,
    ) -> None:
        """Append a message to the tail of one device's directional queue.

        Args:
            device_id: Device this message belongs to. Never crosses over: a
                message is only ever drained onto that device's session.
            direction: Logical queue name (e.g. "local-to-upstream").
            topic: MQTT topic the message belongs to.
            payload: Raw message payload.
            qos: MQTT QoS the message was received/should be sent with.
            coalesce_key: If set, any older pending message for this device
                and direction carrying the same key is discarded first - the
                new message supersedes it. Used for state-carrying commands
                where only the latest value is meaningful (see
                `replay_policy`).
            product_id: Product the device reports itself as.
        """
        with self._lock:
            try:
                with self._connection:
                    if coalesce_key is not None:
                        cursor = self._connection.execute(
                            "DELETE FROM pending_messages WHERE device_id = ? AND direction = ? "
                            "AND coalesce_key = ?",
                            (device_id, direction, coalesce_key),
                        )
                        if cursor.rowcount:
                            _LOGGER.debug(
                                "Superseded %d pending message(s) for %s/%s (key=%s)",
                                cursor.rowcount,
                                device_id,
                                direction,
                                coalesce_key,
                            )
                    self._connection.execute(
                        "INSERT INTO pending_messages "
                        "(device_id, product_id, direction, topic, payload, qos, coalesce_key) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (device_id, product_id, direction, topic, payload, qos, coalesce_key),
                    )
            except sqlite3.Error:
                _LOGGER.exception("Failed to enqueue message for %s on topic %s", direction, topic)
                raise
            self._enforce_size_limit_locked(device_id, direction)

    def _enforce_size_limit_locked(self, device_id: str, direction: str) -> None:
        """Drop the oldest messages of one device's direction beyond the cap.

        The cap is per device and per direction, so one device with a long
        outage can never evict another device's backlog.

        Must be called while holding `self._lock`.
        """
        try:
            with self._connection:
                cursor = self._connection.execute(
                    "SELECT COUNT(*) FROM pending_messages WHERE device_id = ? AND direction = ?",
                    (device_id, direction),
                )
                (current_size,) = cursor.fetchone()
                overflow = current_size - self._max_size_per_direction
                if overflow > 0:
                    self._connection.execute(
                        """
                        DELETE FROM pending_messages WHERE id IN (
                            SELECT id FROM pending_messages
                            WHERE device_id = ? AND direction = ?
                            ORDER BY id ASC
                            LIMIT ?
                        )
                        """,
                        (device_id, direction, overflow),
                    )
                    _LOGGER.warning(
                        "Queue %s for %s exceeded %d pending messages, dropped %d oldest entries",
                        direction,
                        device_id,
                        self._max_size_per_direction,
                        overflow,
                    )
        except sqlite3.Error:
            _LOGGER.exception("Failed to enforce queue size limit for %s", direction)
            raise

    def peek_oldest(self, device_id: str, direction: str) -> QueuedMessage | None:
        """Return one device's oldest pending message, without removing it.

        Args:
            device_id: Device whose queue to read.
            direction: Logical queue name to read from.

        Returns:
            The oldest `QueuedMessage`, or `None` if that queue is empty.
        """
        with self._lock:
            try:
                cursor = self._connection.execute(
                    "SELECT id, topic, payload, qos, created_at FROM pending_messages "
                    "WHERE device_id = ? AND direction = ? ORDER BY id ASC LIMIT 1",
                    (device_id, direction),
                )
                row = cursor.fetchone()
            except sqlite3.Error:
                _LOGGER.exception("Failed to read from queue %s", direction)
                raise
        if row is None:
            return None
        message_id, topic, payload, qos, created_at = row
        return QueuedMessage(
            id=message_id, topic=topic, payload=payload, qos=qos, created_at=float(created_at)
        )

    def remove(self, message_id: int) -> None:
        """Remove a message once it has been successfully delivered.

        Args:
            message_id: Primary key of the message to remove.
        """
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute(
                        "DELETE FROM pending_messages WHERE id = ?", (message_id,)
                    )
            except sqlite3.Error:
                _LOGGER.exception("Failed to remove delivered message %d", message_id)
                raise

    def count(self, device_id: str, direction: str) -> int:
        """Return how many messages are pending for one device and direction."""
        with self._lock:
            try:
                cursor = self._connection.execute(
                    "SELECT COUNT(*) FROM pending_messages WHERE device_id = ? AND direction = ?",
                    (device_id, direction),
                )
                (pending_count,) = cursor.fetchone()
            except sqlite3.Error:
                _LOGGER.exception("Failed to count queue %s", direction)
                raise
        return int(pending_count)

    def depth_by_device(self) -> dict[str, int]:
        """Return total pending messages per device, for the global overview."""
        with self._lock:
            try:
                rows = self._connection.execute(
                    "SELECT device_id, COUNT(*) FROM pending_messages GROUP BY device_id"
                ).fetchall()
            except sqlite3.Error:
                _LOGGER.exception("Failed to aggregate queue depth")
                raise
        return {str(device_id): int(count) for device_id, count in rows if device_id is not None}

    def unroutable_count(self) -> int:
        """Return rows that survived migration without a resolvable device."""
        with self._lock:
            (count,) = self._connection.execute(
                "SELECT COUNT(*) FROM pending_messages WHERE device_id IS NULL"
            ).fetchone()
        return int(count)

    def snapshot(self, device_id: str, direction: str, limit: int = 100) -> dict[str, object]:
        """Return a bounded, metadata-only view of one device's queue direction.

        Raw payloads stay in the State Shadow endpoint. This avoids exposing
        the same potentially sensitive traffic through two unrelated views.
        """
        safe_limit = max(1, min(limit, 500))
        now = time.time()
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, topic, payload, qos, created_at FROM pending_messages "
                "WHERE device_id = ? AND direction = ? ORDER BY id ASC LIMIT ?",
                (device_id, direction, safe_limit),
            ).fetchall()
            aggregate = self._connection.execute(
                "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM pending_messages "
                "WHERE device_id = ? AND direction = ?",
                (device_id, direction),
            ).fetchone()
        total, oldest, newest = aggregate
        from .replay_policy import extract_command, policy_for

        messages = []
        for message_id, topic, payload, qos, created_at in rows:
            command = extract_command(payload)
            policy = policy_for(direction == "upstream-to-local", command)
            policy_name = "LATEST_WINS" if policy.coalesce else (
                "FIFO" if policy.max_age_seconds is None else f"TTL_{int(policy.max_age_seconds)}S"
            )
            messages.append(
                {
                    "id": int(message_id),
                    "topic": topic,
                    "cmd": command,
                    "qos": int(qos),
                    "created_at": float(created_at),
                    "age_seconds": now - float(created_at),
                    "replay_policy": policy_name,
                    "payload": _decode_payload(payload),
                }
            )
        return {
            "device_id": device_id,
            "direction": direction,
            "pending": int(total),
            "oldest_age_seconds": now - float(oldest) if oldest is not None else None,
            "newest_age_seconds": now - float(newest) if newest is not None else None,
            "messages": messages,
        }

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            self._connection.close()


def _decode_payload(payload: bytes) -> object:
    """Return queue payloads for the dashboard's explicit debug view only."""
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")
