"""Durable, disk-persisted FIFO queue used to bridge MQTT outages.

Every message crossing the relay is written here before delivery is attempted.
A separate pump (see `DeliveryPump`) drains the queue as fast as the destination
broker allows. Because the queue lives in a SQLite file on a mounted volume, it
survives both a lost connection and a full container restart: whatever arrived
while the upstream (or local) broker was offline is still there once it comes
back, and gets replayed in the original order.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pending_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,
    topic TEXT NOT NULL,
    payload BLOB NOT NULL,
    qos INTEGER NOT NULL,
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
)
"""
_CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_pending_messages_direction ON pending_messages (direction, id)"
)


@dataclass(frozen=True, slots=True)
class QueuedMessage:
    """A single message pending delivery."""

    id: int
    topic: str
    payload: bytes
    qos: int


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

    def enqueue(self, direction: str, topic: str, payload: bytes, qos: int) -> None:
        """Append a message to the tail of a direction's queue.

        Args:
            direction: Logical queue name (e.g. "local-to-upstream").
            topic: MQTT topic the message belongs to.
            payload: Raw message payload.
            qos: MQTT QoS the message was received/should be sent with.
        """
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute(
                        "INSERT INTO pending_messages (direction, topic, payload, qos) VALUES (?, ?, ?, ?)",
                        (direction, topic, payload, qos),
                    )
            except sqlite3.Error:
                _LOGGER.exception("Failed to enqueue message for %s on topic %s", direction, topic)
                raise
            self._enforce_size_limit_locked(direction)

    def _enforce_size_limit_locked(self, direction: str) -> None:
        """Drop the oldest messages of a direction beyond the configured cap.

        Must be called while holding `self._lock`.
        """
        try:
            with self._connection:
                cursor = self._connection.execute(
                    "SELECT COUNT(*) FROM pending_messages WHERE direction = ?", (direction,)
                )
                (current_size,) = cursor.fetchone()
                overflow = current_size - self._max_size_per_direction
                if overflow > 0:
                    self._connection.execute(
                        """
                        DELETE FROM pending_messages WHERE id IN (
                            SELECT id FROM pending_messages
                            WHERE direction = ?
                            ORDER BY id ASC
                            LIMIT ?
                        )
                        """,
                        (direction, overflow),
                    )
                    _LOGGER.warning(
                        "Queue %s exceeded %d pending messages, dropped %d oldest entries",
                        direction,
                        self._max_size_per_direction,
                        overflow,
                    )
        except sqlite3.Error:
            _LOGGER.exception("Failed to enforce queue size limit for %s", direction)
            raise

    def peek_oldest(self, direction: str) -> QueuedMessage | None:
        """Return the oldest pending message for a direction, without removing it.

        Args:
            direction: Logical queue name to read from.

        Returns:
            The oldest `QueuedMessage`, or `None` if the queue is empty.
        """
        with self._lock:
            try:
                cursor = self._connection.execute(
                    "SELECT id, topic, payload, qos FROM pending_messages "
                    "WHERE direction = ? ORDER BY id ASC LIMIT 1",
                    (direction,),
                )
                row = cursor.fetchone()
            except sqlite3.Error:
                _LOGGER.exception("Failed to read from queue %s", direction)
                raise
        if row is None:
            return None
        message_id, topic, payload, qos = row
        return QueuedMessage(id=message_id, topic=topic, payload=payload, qos=qos)

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

    def count(self, direction: str) -> int:
        """Return the number of messages currently pending for a direction."""
        with self._lock:
            try:
                cursor = self._connection.execute(
                    "SELECT COUNT(*) FROM pending_messages WHERE direction = ?", (direction,)
                )
                (pending_count,) = cursor.fetchone()
            except sqlite3.Error:
                _LOGGER.exception("Failed to count queue %s", direction)
                raise
        return int(pending_count)

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            self._connection.close()
