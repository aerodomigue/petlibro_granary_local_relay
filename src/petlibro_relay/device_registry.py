"""Durable store for device MQTT identities learned from their own CONNECT packets.

`CredentialCaptureProxy` records a device's client ID / username / password
here the first time it sees them; `__main__` reads them back to open the
relay's upstream connection, so the operator never has to extract and
hand-configure credentials via a separate packet capture.

Entries expire. Anything on the LAN can open a connection to the capture
proxy and get its identity recorded, and the most recently seen one is what
the relay adopts - so a stale or bogus entry should not linger forever.
Validating an identity against the cloud before trusting it is deliberately
*not* how this works: the whole point of the relay is to keep running while
the cloud is unreachable, so cloud availability must never gate it. Instead
an identity is simply forgotten once its device has not connected for
`retention_seconds` (72h by default) - long enough to survive an unplugged
weekend, short enough that a one-off bogus entry ages out on its own.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

SECONDS_PER_HOUR = 3600
DEFAULT_RETENTION_HOURS = 72
DEFAULT_RETENTION_SECONDS = DEFAULT_RETENTION_HOURS * SECONDS_PER_HOUR

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS device_identities (
    client_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    password TEXT NOT NULL,
    first_seen_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    last_seen_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
)
"""


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """A device's MQTT client ID, username and password."""

    client_id: str
    username: str
    password: str


class DeviceRegistry:
    """SQLite-backed store of device identities learned from live traffic."""

    def __init__(self, db_path: str, retention_seconds: float = DEFAULT_RETENTION_SECONDS) -> None:
        """Open (or create) the registry database.

        Args:
            db_path: Filesystem path to the SQLite database file.
            retention_seconds: Identities whose device has not connected
                within this window are forgotten.
        """
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._retention_seconds = retention_seconds
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(db_path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        with self._connection:
            self._connection.execute(_CREATE_TABLE_SQL)

    def record(self, identity: DeviceIdentity) -> None:
        """Insert or refresh a learned device identity.

        Args:
            identity: The client ID, username and password observed on a
                real CONNECT packet.
        """
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute(
                        """
                        INSERT INTO device_identities (client_id, username, password)
                        VALUES (?, ?, ?)
                        ON CONFLICT(client_id) DO UPDATE SET
                            username = excluded.username,
                            password = excluded.password,
                            last_seen_at = strftime('%s', 'now')
                        """,
                        (identity.client_id, identity.username, identity.password),
                    )
            except sqlite3.Error:
                _LOGGER.exception("Failed to record device identity for %s", identity.client_id)
                raise
        _LOGGER.info("Learned device identity: client_id=%s", identity.client_id)

    def purge_expired(self) -> int:
        """Forget identities whose device has not connected within the retention window.

        Returns:
            How many identities were removed.
        """
        cutoff = time.time() - self._retention_seconds
        with self._lock:
            try:
                with self._connection:
                    cursor = self._connection.execute(
                        "DELETE FROM device_identities WHERE last_seen_at < ?", (cutoff,)
                    )
                    removed = cursor.rowcount
            except sqlite3.Error:
                _LOGGER.exception("Failed to purge expired device identities")
                raise
        if removed:
            _LOGGER.info(
                "Forgot %d device identit%s not seen in the last %.0fh",
                removed,
                "y" if removed == 1 else "ies",
                self._retention_seconds / SECONDS_PER_HOUR,
            )
        return removed

    def get_most_recently_seen(self) -> DeviceIdentity | None:
        """Return the most recently seen non-expired device identity, if any.

        Returns:
            The `DeviceIdentity` with the latest `last_seen_at` within the
            retention window, or `None` if none qualifies.
        """
        cutoff = time.time() - self._retention_seconds
        with self._lock:
            try:
                cursor = self._connection.execute(
                    "SELECT client_id, username, password FROM device_identities "
                    "WHERE last_seen_at >= ? ORDER BY last_seen_at DESC LIMIT 1",
                    (cutoff,),
                )
                row = cursor.fetchone()
            except sqlite3.Error:
                _LOGGER.exception("Failed to read device registry")
                raise
        if row is None:
            return None
        client_id, username, password = row
        return DeviceIdentity(client_id=client_id, username=username, password=password)

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            self._connection.close()
