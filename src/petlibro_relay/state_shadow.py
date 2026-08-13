"""Persistent view of what the feeder reports and what the cloud last asked for.

This replaces the previous "last raw payload per topic, rewritten as one JSON
file on every message" cache with something the relay can actually reason
about, stored in SQLite alongside the other durable state.

Three kinds of knowledge are kept apart, because they have different owners
and different trust levels:

* **reported** - physical facts the device tells us (heartbeat, firmware,
  grain output, errors). The device is the source of truth; the relay only
  observes and stores. It never invents these.
* **desired** - settings and configuration the cloud last pushed. The cloud
  is the source of truth; the newest valid cloud message becomes the
  last-known-good the relay may serve back while the cloud is unreachable.
* **local confirmed** - settings an interactive local control asked for and
  the feeder explicitly acknowledged. This is intentionally distinct from
  cloud desired state: it proves device acceptance, not that the cloud has
  made the setting authoritative.
* **feeding plans** - the last *complete* plan set the cloud sent, kept whole
  rather than merged, so a stale partial set can never be replayed as if it
  were current.

Anything not understood is still archived verbatim in `raw_messages` (last
payload per topic) so unknown traffic remains inspectable without pretending
to interpret it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

_CREATE_REPORTED_SQL = """
CREATE TABLE IF NOT EXISTS device_reported (
    device_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (device_id, key)
)
"""
_CREATE_DESIRED_SQL = """
CREATE TABLE IF NOT EXISTS cloud_desired (
    device_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (device_id, key)
)
"""
_CREATE_LOCAL_CONFIRMED_SQL = """
CREATE TABLE IF NOT EXISTS local_confirmed (
    device_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (device_id, key)
)
"""
_CREATE_PLANS_SQL = """
CREATE TABLE IF NOT EXISTS feeding_plans (
    device_id TEXT PRIMARY KEY,
    plans_json TEXT NOT NULL,
    source_msg_id TEXT,
    updated_at REAL NOT NULL
)
"""
_CREATE_RAW_SQL = """
CREATE TABLE IF NOT EXISTS raw_messages (
    device_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    payload BLOB NOT NULL,
    cmd TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (device_id, topic)
)
"""


@dataclass(frozen=True, slots=True)
class FeedingPlans:
    """The last complete feeding plan set received from the cloud."""

    plans: list[dict[str, Any]]
    source_msg_id: str | None
    updated_at: float


class StateShadow:
    """SQLite-backed shadow of device-reported and cloud-desired state."""

    def __init__(self, db_path: str) -> None:
        """Open (or create) the state shadow database.

        Args:
            db_path: Filesystem path to the SQLite database file.
        """
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(db_path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        with self._connection:
            for statement in (
                _CREATE_REPORTED_SQL,
                _CREATE_DESIRED_SQL,
                _CREATE_LOCAL_CONFIRMED_SQL,
                _CREATE_PLANS_SQL,
                _CREATE_RAW_SQL,
            ):
                self._connection.execute(statement)

    # -- writes ------------------------------------------------------------------

    def record_raw(self, device_id: str, topic: str, payload: bytes, command: str | None) -> None:
        """Archive the latest raw payload seen on a topic."""
        self._upsert(
            "INSERT INTO raw_messages (device_id, topic, payload, cmd, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(device_id, topic) DO UPDATE SET "
            "payload = excluded.payload, cmd = excluded.cmd, updated_at = excluded.updated_at",
            (device_id, topic, payload, command, time.time()),
        )

    def update_reported(self, device_id: str, values: dict[str, Any]) -> None:
        """Record physical facts the device reported about itself."""
        self._update_kv("device_reported", device_id, values)

    def update_desired(self, device_id: str, values: dict[str, Any]) -> None:
        """Record settings the cloud last pushed for this device."""
        self._update_kv("cloud_desired", device_id, values)
        self._delete_keys("local_confirmed", device_id, values.keys())
        _LOGGER.info("CACHE UPDATE config (%d key(s))", len(values))

    def update_local_confirmed(self, device_id: str, values: dict[str, Any]) -> None:
        """Record a setting only after the physical feeder acknowledged it."""
        self._update_kv("local_confirmed", device_id, values)

    def update_feeding_plans(
        self, device_id: str, plans: list[dict[str, Any]], source_msg_id: str | None
    ) -> None:
        """Replace the stored plan set with a complete one from the cloud.

        Stored whole rather than merged: a plan set only means something as a
        complete list, and merging could resurrect a plan the cloud deleted.
        """
        self._upsert(
            "INSERT INTO feeding_plans (device_id, plans_json, source_msg_id, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET "
            "plans_json = excluded.plans_json, source_msg_id = excluded.source_msg_id, "
            "updated_at = excluded.updated_at",
            (device_id, json.dumps(plans), source_msg_id, time.time()),
        )
        _LOGGER.info("CACHE UPDATE feeding_plan (%d plan(s))", len(plans))

    def _update_kv(self, table: str, device_id: str, values: dict[str, Any]) -> None:
        now = time.time()
        rows = [(device_id, key, json.dumps(value), now) for key, value in values.items()]
        if not rows:
            return
        with self._lock:
            try:
                with self._connection:
                    self._connection.executemany(
                        f"INSERT INTO {table} (device_id, key, value, updated_at) "  # noqa: S608 - fixed table names
                        "VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(device_id, key) DO UPDATE SET "
                        "value = excluded.value, updated_at = excluded.updated_at",
                        rows,
                    )
            except sqlite3.Error:
                _LOGGER.exception("Failed to update %s for %s", table, device_id)
                raise

    def _upsert(self, statement: str, parameters: tuple[Any, ...]) -> None:
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute(statement, parameters)
            except sqlite3.Error:
                _LOGGER.exception("Failed to write state shadow")
                raise

    def _delete_keys(self, table: str, device_id: str, keys: Iterable[str]) -> None:
        """Remove local confirmations superseded by a cloud desired-state push."""
        key_list = list(keys)
        if not key_list:
            return
        placeholders = ",".join("?" for _ in key_list)
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute(
                        f"DELETE FROM {table} WHERE device_id = ? AND key IN ({placeholders})",  # noqa: S608 - fixed table name
                        (device_id, *key_list),
                    )
            except sqlite3.Error:
                _LOGGER.exception("Failed to clear superseded %s for %s", table, device_id)
                raise

    # -- reads -------------------------------------------------------------------

    def get_desired(self, device_id: str) -> dict[str, Any]:
        """Return the last cloud-pushed settings for a device."""
        return self._read_kv("cloud_desired", device_id)

    def get_reported(self, device_id: str) -> dict[str, Any]:
        """Return the last device-reported facts."""
        return self._read_kv("device_reported", device_id)

    def get_local_confirmed(self, device_id: str) -> dict[str, Any]:
        """Return settings the feeder confirmed from a local interactive write."""
        return self._read_kv("local_confirmed", device_id)

    def _read_kv(self, table: str, device_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                rows = self._connection.execute(
                    f"SELECT key, value FROM {table} WHERE device_id = ?",  # noqa: S608 - fixed table names
                    (device_id,),
                ).fetchall()
            except sqlite3.Error:
                _LOGGER.exception("Failed to read %s for %s", table, device_id)
                raise
        return {key: json.loads(value) for key, value in rows}

    def get_feeding_plans(self, device_id: str) -> FeedingPlans | None:
        """Return the last complete plan set from the cloud, if one is known."""
        with self._lock:
            try:
                row = self._connection.execute(
                    "SELECT plans_json, source_msg_id, updated_at FROM feeding_plans "
                    "WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
            except sqlite3.Error:
                _LOGGER.exception("Failed to read feeding plans for %s", device_id)
                raise
        if row is None:
            return None
        plans_json, source_msg_id, updated_at = row
        return FeedingPlans(
            plans=json.loads(plans_json), source_msg_id=source_msg_id, updated_at=float(updated_at)
        )

    def snapshot(self, device_id: str) -> dict[str, Any]:
        """Return a human-readable view of everything known about a device."""
        plans = self.get_feeding_plans(device_id)
        return {
            "device_id": device_id,
            "reported": self.get_reported(device_id),
            "desired": self.get_desired(device_id),
            "local_confirmed": self.get_local_confirmed(device_id),
            "feeding_plans": {
                "plans": plans.plans if plans else [],
                "updated_at": plans.updated_at if plans else None,
            },
        }

    def dashboard_snapshot(self, device_id: str, raw_limit: int = 100) -> dict[str, Any]:
        """Return bounded state with timestamps and safe raw JSON previews."""
        safe_limit = max(1, min(raw_limit, 500))
        with self._lock:
            reported_rows = self._connection.execute(
                "SELECT key, value, updated_at FROM device_reported WHERE device_id = ? ORDER BY key",
                (device_id,),
            ).fetchall()
            desired_rows = self._connection.execute(
                "SELECT key, value, updated_at FROM cloud_desired WHERE device_id = ? ORDER BY key",
                (device_id,),
            ).fetchall()
            local_confirmed_rows = self._connection.execute(
                "SELECT key, value, updated_at FROM local_confirmed WHERE device_id = ? ORDER BY key",
                (device_id,),
            ).fetchall()
            raw_rows = self._connection.execute(
                "SELECT topic, payload, cmd, updated_at FROM raw_messages WHERE device_id = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (device_id, safe_limit),
            ).fetchall()
            raw_count = self._connection.execute(
                "SELECT COUNT(*) FROM raw_messages WHERE device_id = ?", (device_id,)
            ).fetchone()[0]
        plans = self.get_feeding_plans(device_id)
        return {
            "device_id": device_id,
            "reported": [
                {"key": key, "value": json.loads(value), "updated_at": float(updated_at)}
                for key, value, updated_at in reported_rows
            ],
            "desired": [
                {"key": key, "value": json.loads(value), "updated_at": float(updated_at)}
                for key, value, updated_at in desired_rows
            ],
            "local_confirmed": [
                {"key": key, "value": json.loads(value), "updated_at": float(updated_at)}
                for key, value, updated_at in local_confirmed_rows
            ],
            "feeding_plans": {
                "plans": plans.plans if plans else [],
                "source_msg_id": plans.source_msg_id if plans else None,
                "updated_at": plans.updated_at if plans else None,
                "complete": plans is not None,
            },
            "raw_messages": [
                {
                    "topic": topic,
                    "cmd": command,
                    "payload": _decode_raw_payload(payload),
                    "updated_at": float(updated_at),
                }
                for topic, payload, command, updated_at in raw_rows
            ],
            "counts": {
                "reported": len(reported_rows),
                "desired": len(desired_rows),
                "local_confirmed": len(local_confirmed_rows),
                "raw_messages": int(raw_count),
                "feeding_plan_cached": plans is not None,
            },
        }

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            self._connection.close()


def _decode_raw_payload(payload: bytes) -> Any:
    """Decode JSON raw traffic when possible; otherwise expose a safe text form."""
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")
