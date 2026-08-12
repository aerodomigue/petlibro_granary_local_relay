"""Durable store for device MQTT identities learned from their own CONNECT packets.

`CredentialCaptureProxy` records a device's client ID / username / password
here the first time it sees them; `__main__` reads them back to open the
relay's upstream connection, so the operator never has to extract and
hand-configure credentials via a separate packet capture.

## Active vs candidate

Anything on the LAN can reach the capture proxy and get itself recorded, so
"whichever identity was seen most recently" is not a safe way to decide which
device the relay bridges: a test client - or a second feeder - connecting
once would displace the real device.

The registry therefore keeps a *sticky* active identity. The first identity
learned becomes active and stays active; any different client_id seen
afterwards is stored as a candidate and logged, but never takes over on its
own. The active identity only loses its role by going quiet: once its device
has not connected for `retention_seconds` (72h by default), it expires, and
the next device to connect can take the role.

The active identity is a pointer (`registry_state.active_client_id`) rather
than a per-row status flag, so "exactly one active" is guaranteed by the
schema instead of by careful bookkeeping.

## No cloud involvement

An identity is never validated against the PETLIBRO cloud before being
trusted. The relay exists to keep working while that cloud is unreachable -
and it has been observed accepting TCP then never sending a CONNACK for ~30s
before resetting - so cloud reachability must never gate whether the relay
can start.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

SECONDS_PER_HOUR = 3600
DEFAULT_RETENTION_HOURS = 72
DEFAULT_RETENTION_SECONDS = DEFAULT_RETENTION_HOURS * SECONDS_PER_HOUR

ACTIVE_CLIENT_ID_KEY = "active_client_id"

_CREATE_IDENTITIES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS device_identities (
    client_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    password TEXT NOT NULL,
    first_seen_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    last_seen_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
)
"""
_CREATE_STATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS registry_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


class RecordOutcome(Enum):
    """What recording an identity did to the active role."""

    PROMOTED_TO_ACTIVE = "promoted_to_active"
    REFRESHED_ACTIVE = "refreshed_active"
    STORED_AS_CANDIDATE = "stored_as_candidate"


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """A device's MQTT client ID, username and password."""

    client_id: str
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class DeviceRegistryEntry:
    """Non-secret device identity metadata for observability."""

    client_id: str
    username: str
    first_seen_at: float
    last_seen_at: float
    active: bool


class DeviceRegistry:
    """SQLite-backed store of device identities, with a sticky active device."""

    def __init__(self, db_path: str, retention_seconds: float = DEFAULT_RETENTION_SECONDS) -> None:
        """Open (or create) the registry database.

        Args:
            db_path: Filesystem path to the SQLite database file.
            retention_seconds: An identity whose device has not connected
                within this window expires; an expired active identity gives
                up the active role.
        """
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._retention_seconds = retention_seconds
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(db_path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        with self._connection:
            self._connection.execute(_CREATE_IDENTITIES_TABLE_SQL)
            self._connection.execute(_CREATE_STATE_TABLE_SQL)
            self._adopt_pre_existing_active_locked()

    # -- schema / migration ------------------------------------------------------

    def _adopt_pre_existing_active_locked(self) -> None:
        """Give the active role to an identity learned before this table existed.

        Databases written by an earlier version have identities but no
        `registry_state`. Leaving the active pointer empty would make the
        relay wait for a fresh CONNECT even though it already knows the
        device, so the most recently seen non-expired identity inherits the
        role once, at migration time.
        """
        if self._read_state_locked(ACTIVE_CLIENT_ID_KEY) is not None:
            return
        cursor = self._connection.execute(
            "SELECT client_id FROM device_identities WHERE last_seen_at >= ? "
            "ORDER BY last_seen_at DESC LIMIT 1",
            (self._cutoff(),),
        )
        row = cursor.fetchone()
        if row is None:
            return
        self._write_state_locked(ACTIVE_CLIENT_ID_KEY, row[0])
        _LOGGER.info("Migrated registry: adopted previously learned device %s as active", row[0])

    # -- state helpers -----------------------------------------------------------

    def _cutoff(self) -> float:
        return time.time() - self._retention_seconds

    def _read_state_locked(self, key: str) -> str | None:
        cursor = self._connection.execute("SELECT value FROM registry_state WHERE key = ?", (key,))
        row = cursor.fetchone()
        return str(row[0]) if row is not None else None

    def _write_state_locked(self, key: str, value: str) -> None:
        self._connection.execute(
            "INSERT INTO registry_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _clear_state_locked(self, key: str) -> None:
        self._connection.execute("DELETE FROM registry_state WHERE key = ?", (key,))

    def _is_fresh_locked(self, client_id: str) -> bool:
        cursor = self._connection.execute(
            "SELECT 1 FROM device_identities WHERE client_id = ? AND last_seen_at >= ?",
            (client_id, self._cutoff()),
        )
        return cursor.fetchone() is not None

    # -- public API --------------------------------------------------------------

    def record(self, identity: DeviceIdentity) -> RecordOutcome:
        """Record an identity seen on a real CONNECT, and resolve the active role.

        The identity's credentials and `last_seen_at` are always stored -
        credentials can legitimately change, and a candidate's freshness still
        matters. Whether it becomes the active device follows the sticky rule
        described in this module's docstring.

        Args:
            identity: The client ID, username and password observed.

        Returns:
            What this did to the active role.
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

                    active_client_id = self._read_state_locked(ACTIVE_CLIENT_ID_KEY)
                    if active_client_id == identity.client_id:
                        outcome = RecordOutcome.REFRESHED_ACTIVE
                    elif active_client_id is None or not self._is_fresh_locked(active_client_id):
                        # No active device, or the incumbent has gone quiet past
                        # the retention window and forfeits the role.
                        self._write_state_locked(ACTIVE_CLIENT_ID_KEY, identity.client_id)
                        outcome = RecordOutcome.PROMOTED_TO_ACTIVE
                    else:
                        outcome = RecordOutcome.STORED_AS_CANDIDATE
            except sqlite3.Error:
                _LOGGER.exception("Failed to record device identity for %s", identity.client_id)
                raise

        if outcome is RecordOutcome.PROMOTED_TO_ACTIVE:
            _LOGGER.info("Learned device identity: client_id=%s is now the active device", identity.client_id)
        elif outcome is RecordOutcome.REFRESHED_ACTIVE:
            _LOGGER.debug("Refreshed active device identity: client_id=%s", identity.client_id)
        else:
            _LOGGER.warning(
                "Learned foreign candidate device %s; active device remains %s. "
                "This relay bridges one device - run a second instance for another feeder.",
                identity.client_id,
                active_client_id,
            )
        return outcome

    def get_active(self) -> DeviceIdentity | None:
        """Return the active device identity, if there is a non-expired one.

        Returns:
            The active `DeviceIdentity`, or `None` if none has been learned
            yet or the incumbent has expired.
        """
        with self._lock:
            try:
                active_client_id = self._read_state_locked(ACTIVE_CLIENT_ID_KEY)
                if active_client_id is None:
                    return None
                cursor = self._connection.execute(
                    "SELECT client_id, username, password FROM device_identities "
                    "WHERE client_id = ? AND last_seen_at >= ?",
                    (active_client_id, self._cutoff()),
                )
                row = cursor.fetchone()
            except sqlite3.Error:
                _LOGGER.exception("Failed to read device registry")
                raise
        if row is None:
            return None
        client_id, username, password = row
        return DeviceIdentity(client_id=client_id, username=username, password=password)

    def get_candidates(self) -> list[DeviceIdentity]:
        """Return every known non-expired identity that is not the active one."""
        with self._lock:
            try:
                active_client_id = self._read_state_locked(ACTIVE_CLIENT_ID_KEY) or ""
                rows = self._connection.execute(
                    "SELECT client_id, username, password FROM device_identities "
                    "WHERE client_id != ? AND last_seen_at >= ? ORDER BY last_seen_at DESC",
                    (active_client_id, self._cutoff()),
                ).fetchall()
            except sqlite3.Error:
                _LOGGER.exception("Failed to list candidate device identities")
                raise
        return [DeviceIdentity(client_id=r[0], username=r[1], password=r[2]) for r in rows]

    def snapshot(self) -> dict[str, object]:
        """Return active/candidate metadata without exposing passwords."""
        now = time.time()
        with self._lock:
            active_client_id = self._read_state_locked(ACTIVE_CLIENT_ID_KEY)
            rows = self._connection.execute(
                "SELECT client_id, username, first_seen_at, last_seen_at FROM device_identities "
                "ORDER BY last_seen_at DESC"
            ).fetchall()
        entries = [
            DeviceRegistryEntry(
                client_id=str(client_id),
                username=str(username),
                first_seen_at=float(first_seen_at),
                last_seen_at=float(last_seen_at),
                active=client_id == active_client_id,
            )
            for client_id, username, first_seen_at, last_seen_at in rows
        ]
        active = next((entry for entry in entries if entry.active and now - entry.last_seen_at <= self._retention_seconds), None)
        return {
            "active": active,
            "candidates": [entry for entry in entries if not entry.active and now - entry.last_seen_at <= self._retention_seconds],
            "retention_seconds": self._retention_seconds,
        }

    def purge_expired(self) -> int:
        """Forget identities whose device has not connected within the retention window.

        Also clears the active pointer if it referred to an identity that just
        expired, so the role is genuinely vacant for the next device.

        Returns:
            How many identities were removed.
        """
        with self._lock:
            try:
                with self._connection:
                    cursor = self._connection.execute(
                        "DELETE FROM device_identities WHERE last_seen_at < ?", (self._cutoff(),)
                    )
                    removed = cursor.rowcount
                    active_client_id = self._read_state_locked(ACTIVE_CLIENT_ID_KEY)
                    if active_client_id is not None and not self._is_fresh_locked(active_client_id):
                        self._clear_state_locked(ACTIVE_CLIENT_ID_KEY)
                        _LOGGER.info(
                            "Active device %s expired; the role is now vacant", active_client_id
                        )
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

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            self._connection.close()
