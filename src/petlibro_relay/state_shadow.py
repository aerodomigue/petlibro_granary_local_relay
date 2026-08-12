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
        _LOGGER.info("CACHE UPDATE config (%d key(s))", len(values))

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

    # -- reads -------------------------------------------------------------------

    def get_desired(self, device_id: str) -> dict[str, Any]:
        """Return the last cloud-pushed settings for a device."""
        return self._read_kv("cloud_desired", device_id)

    def get_reported(self, device_id: str) -> dict[str, Any]:
        """Return the last device-reported facts."""
        return self._read_kv("device_reported", device_id)

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
            "feeding_plans": {
                "plans": plans.plans if plans else [],
                "updated_at": plans.updated_at if plans else None,
            },
        }

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            self._connection.close()
