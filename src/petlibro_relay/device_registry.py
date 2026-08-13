"""Durable store for device MQTT identities learned from their own CONNECT packets.

`CredentialCaptureProxy` records a device's client ID / username / password
here the first time it sees them; `DeviceManager` reads them back to open one
upstream connection per device, so the operator never has to extract and
hand-configure credentials via a separate packet capture.

## Enrollment

Anything on the LAN can reach the capture proxy and get itself recorded, so
being *seen* is not the same as being *trusted*. Each identity therefore
carries a status:

* `KNOWN` - enrolled. The relay opens an upstream session for it.
* `CANDIDATE` - seen, stored, shown on the dashboard, but never bridged.
* `DISABLED` - explicitly held back; never bridged, never auto-promoted.

`PETLIBRO_AUTO_ENROLL` (default on) decides which of the first two a newly
learned device lands in. With auto-enrollment off, a new feeder is recorded as
a candidate and simply waits - adding it is then a deliberate act rather than
a consequence of plugging something into the LAN.

Unlike the single-active model this replaces, enrolling one device never
displaces another: N devices are bridged concurrently, each with its own
identity and its own upstream session.

## Expiry

An identity whose device has not connected within `retention_seconds` (72h by
default) is forgotten entirely. That is the only way an entry leaves the
registry on its own.

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
from enum import Enum, StrEnum
from pathlib import Path

from . import protocol

_LOGGER = logging.getLogger(__name__)

SECONDS_PER_HOUR = 3600
DEFAULT_RETENTION_HOURS = 72
DEFAULT_RETENTION_SECONDS = DEFAULT_RETENTION_HOURS * SECONDS_PER_HOUR

# Key of the single-active pointer used by the pre-multi-device schema. Read
# once at migration time to decide which identity was the bridged one, then
# left alone.
LEGACY_ACTIVE_CLIENT_ID_KEY = "active_client_id"

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
_ADD_PRODUCT_ID_COLUMN_SQL = "ALTER TABLE device_identities ADD COLUMN product_id TEXT"
_ADD_STATUS_COLUMN_SQL = "ALTER TABLE device_identities ADD COLUMN status TEXT"
_ADD_ENABLED_COLUMN_SQL = "ALTER TABLE device_identities ADD COLUMN enabled INTEGER"


class DeviceStatus(StrEnum):
    """Whether an identity may be bridged to the PETLIBRO cloud."""

    KNOWN = "KNOWN"
    CANDIDATE = "CANDIDATE"
    DISABLED = "DISABLED"


class RecordOutcome(Enum):
    """What recording an identity did to the registry."""

    ENROLLED = "enrolled"
    REFRESHED = "refreshed"
    PENDING_APPROVAL = "pending_approval"
    HELD_DISABLED = "held_disabled"


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """A device's MQTT client ID, username and password.

    `client_id` doubles as the device id: the feeder uses the same value in
    its CONNECT packet and in the topics it publishes on.
    """

    client_id: str
    username: str
    password: str
    product_id: str = protocol.PRODUCT_ID


@dataclass(frozen=True, slots=True)
class DeviceRegistryEntry:
    """Non-secret device identity metadata for observability."""

    client_id: str
    username: str
    product_id: str
    first_seen_at: float
    last_seen_at: float
    status: DeviceStatus
    enabled: bool

    @property
    def bridged(self) -> bool:
        """True if this identity is eligible for an upstream session."""
        return self.enabled and self.status is DeviceStatus.KNOWN


class DeviceRegistry:
    """SQLite-backed store of every device identity the relay has learned."""

    def __init__(
        self,
        db_path: str,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        auto_enroll: bool = True,
    ) -> None:
        """Open (or create) the registry database.

        Args:
            db_path: Filesystem path to the SQLite database file.
            retention_seconds: An identity whose device has not connected
                within this window is forgotten.
            auto_enroll: Whether a newly learned device is enrolled as `KNOWN`
                straight away, or parked as a `CANDIDATE`.
        """
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._retention_seconds = retention_seconds
        self._auto_enroll = auto_enroll
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(db_path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        with self._connection:
            self._connection.execute(_CREATE_IDENTITIES_TABLE_SQL)
            self._connection.execute(_CREATE_STATE_TABLE_SQL)
            self._migrate_to_multi_device_locked()

    # -- schema / migration ------------------------------------------------------

    def _migrate_to_multi_device_locked(self) -> None:
        """Add the multi-device columns and carry over the old active pointer.

        The pre-multi-device schema stored which single device was bridged in
        `registry_state.active_client_id`. That decision is preserved exactly:
        the identity that was active becomes `KNOWN`, and the ones that were
        only candidates stay `CANDIDATE` even when auto-enrollment is on.
        Anything else would silently start bridging a device the previous
        version deliberately refused to bridge.
        """
        cursor = self._connection.execute("PRAGMA table_info(device_identities)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        added_columns = False
        for column, statement in (
            ("product_id", _ADD_PRODUCT_ID_COLUMN_SQL),
            ("status", _ADD_STATUS_COLUMN_SQL),
            ("enabled", _ADD_ENABLED_COLUMN_SQL),
        ):
            if column not in existing_columns:
                self._connection.execute(statement)
                added_columns = True
        if not added_columns:
            return

        legacy_active = self._read_state_locked(LEGACY_ACTIVE_CLIENT_ID_KEY)
        self._connection.execute(
            "UPDATE device_identities SET product_id = ? WHERE product_id IS NULL",
            (protocol.PRODUCT_ID,),
        )
        self._connection.execute(
            "UPDATE device_identities SET status = ?, enabled = 1 WHERE status IS NULL "
            "AND client_id = ?",
            (DeviceStatus.KNOWN.value, legacy_active or ""),
        )
        cursor = self._connection.execute(
            "UPDATE device_identities SET status = ?, enabled = 0 WHERE status IS NULL",
            (DeviceStatus.CANDIDATE.value,),
        )
        _LOGGER.info(
            "Migrated registry to multi-device: kept %s enrolled, parked %d other identit%s "
            "as candidates",
            legacy_active or "no previously active device",
            cursor.rowcount,
            "y" if cursor.rowcount == 1 else "ies",
        )

    # -- state helpers -----------------------------------------------------------

    def _cutoff(self) -> float:
        return time.time() - self._retention_seconds

    def _read_state_locked(self, key: str) -> str | None:
        cursor = self._connection.execute("SELECT value FROM registry_state WHERE key = ?", (key,))
        row = cursor.fetchone()
        return str(row[0]) if row is not None else None

    # -- public API --------------------------------------------------------------

    def record(self, identity: DeviceIdentity) -> RecordOutcome:
        """Record an identity seen on a real CONNECT and resolve its status.

        Credentials and `last_seen_at` are always refreshed - credentials can
        legitimately change, and a candidate's freshness still matters. An
        identity already in the registry keeps whatever status it has: seeing
        a device again never re-opens the enrollment decision, in either
        direction.

        Args:
            identity: The client ID, username, password and product observed.

        Returns:
            What this did to the identity.
        """
        with self._lock:
            try:
                with self._connection:
                    existing_status = self._status_of_locked(identity.client_id)
                    if existing_status is None:
                        status = (
                            DeviceStatus.KNOWN if self._auto_enroll else DeviceStatus.CANDIDATE
                        )
                        outcome = (
                            RecordOutcome.ENROLLED
                            if self._auto_enroll
                            else RecordOutcome.PENDING_APPROVAL
                        )
                    else:
                        status = existing_status
                        outcome = (
                            RecordOutcome.REFRESHED
                            if status is DeviceStatus.KNOWN
                            else RecordOutcome.HELD_DISABLED
                            if status is DeviceStatus.DISABLED
                            else RecordOutcome.PENDING_APPROVAL
                        )
                    self._connection.execute(
                        """
                        INSERT INTO device_identities
                            (client_id, username, password, product_id, status, enabled)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(client_id) DO UPDATE SET
                            username = excluded.username,
                            password = excluded.password,
                            product_id = excluded.product_id,
                            last_seen_at = strftime('%s', 'now')
                        """,
                        (
                            identity.client_id,
                            identity.username,
                            identity.password,
                            identity.product_id,
                            status.value,
                            1 if status is DeviceStatus.KNOWN else 0,
                        ),
                    )
            except sqlite3.Error:
                _LOGGER.exception("Failed to record device identity for %s", identity.client_id)
                raise

        self._log_outcome(identity, outcome)
        return outcome

    def _status_of_locked(self, client_id: str) -> DeviceStatus | None:
        cursor = self._connection.execute(
            "SELECT status FROM device_identities WHERE client_id = ?", (client_id,)
        )
        row = cursor.fetchone()
        if row is None or row[0] is None:
            return None
        return DeviceStatus(str(row[0]))

    def _log_outcome(self, identity: DeviceIdentity, outcome: RecordOutcome) -> None:
        if outcome is RecordOutcome.ENROLLED:
            _LOGGER.info(
                "Learned device identity: client_id=%s product=%s is now enrolled",
                identity.client_id,
                identity.product_id,
            )
        elif outcome is RecordOutcome.REFRESHED:
            _LOGGER.debug("Refreshed enrolled device identity: client_id=%s", identity.client_id)
        elif outcome is RecordOutcome.PENDING_APPROVAL:
            _LOGGER.warning(
                "Device %s is a candidate and will not be bridged. Auto-enrollment is off; "
                "set PETLIBRO_AUTO_ENROLL=true to bridge newly learned devices.",
                identity.client_id,
            )
        else:
            _LOGGER.warning("Device %s is disabled and will not be bridged", identity.client_id)

    def get_bridgeable(self) -> list[DeviceIdentity]:
        """Return every non-expired identity the relay may open a session for."""
        with self._lock:
            try:
                rows = self._connection.execute(
                    "SELECT client_id, username, password, product_id FROM device_identities "
                    "WHERE status = ? AND enabled = 1 AND last_seen_at >= ? "
                    "ORDER BY first_seen_at ASC",
                    (DeviceStatus.KNOWN.value, self._cutoff()),
                ).fetchall()
            except sqlite3.Error:
                _LOGGER.exception("Failed to list bridgeable device identities")
                raise
        return [
            DeviceIdentity(
                client_id=client_id,
                username=username,
                password=password,
                product_id=product_id or protocol.PRODUCT_ID,
            )
            for client_id, username, password, product_id in rows
        ]

    def get(self, client_id: str) -> DeviceIdentity | None:
        """Return one identity by client id, expired or not."""
        with self._lock:
            try:
                row = self._connection.execute(
                    "SELECT client_id, username, password, product_id FROM device_identities "
                    "WHERE client_id = ?",
                    (client_id,),
                ).fetchone()
            except sqlite3.Error:
                _LOGGER.exception("Failed to read device identity %s", client_id)
                raise
        if row is None:
            return None
        return DeviceIdentity(
            client_id=row[0], username=row[1], password=row[2], product_id=row[3] or protocol.PRODUCT_ID
        )

    def entries(self) -> list[DeviceRegistryEntry]:
        """Return non-secret metadata for every non-expired identity."""
        with self._lock:
            try:
                rows = self._connection.execute(
                    "SELECT client_id, username, product_id, first_seen_at, last_seen_at, "
                    "status, enabled FROM device_identities WHERE last_seen_at >= ? "
                    "ORDER BY first_seen_at ASC",
                    (self._cutoff(),),
                ).fetchall()
            except sqlite3.Error:
                _LOGGER.exception("Failed to list device identities")
                raise
        return [
            DeviceRegistryEntry(
                client_id=str(client_id),
                username=str(username),
                product_id=str(product_id or protocol.PRODUCT_ID),
                first_seen_at=float(first_seen_at),
                last_seen_at=float(last_seen_at),
                status=DeviceStatus(str(status)),
                enabled=bool(enabled),
            )
            for client_id, username, product_id, first_seen_at, last_seen_at, status, enabled in rows
        ]

    @property
    def retention_seconds(self) -> float:
        """Return how long an identity survives without its device connecting."""
        return self._retention_seconds

    @property
    def auto_enroll(self) -> bool:
        """Return whether newly learned devices are bridged without approval."""
        return self._auto_enroll

    def purge_expired(self) -> int:
        """Forget identities whose device has not connected within the retention window.

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
