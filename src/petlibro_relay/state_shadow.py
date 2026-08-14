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
_CREATE_SCHEDULE_PLANS_SQL = """
CREATE TABLE IF NOT EXISTS schedule_plans (
    device_id TEXT NOT NULL,
    plan_id INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('cloud', 'local')),
    updated_at REAL NOT NULL,
    PRIMARY KEY (device_id, plan_id)
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
_CREATE_CAMERA_UIDS_SQL = """
CREATE TABLE IF NOT EXISTS camera_uids (
    device_id TEXT PRIMARY KEY,
    uid TEXT NOT NULL,
    updated_at REAL NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class FeedingPlans:
    """The last complete feeding plan set received from the cloud."""

    plans: list[dict[str, Any]]
    source_msg_id: str | None
    updated_at: float


@dataclass(frozen=True, slots=True)
class SchedulePlan:
    """One editable feeding plan scoped to a feeder and its source."""

    plan: dict[str, Any]
    source: str
    updated_at: float


@dataclass(frozen=True, slots=True)
class CameraUID:
    """A camera UID learned from the feeder's device-start event."""

    device_id: str
    uid: str
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
                _CREATE_SCHEDULE_PLANS_SQL,
                _CREATE_RAW_SQL,
                _CREATE_CAMERA_UIDS_SQL,
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

    def record_camera_uid(self, device_id: str, uid: str) -> bool:
        """Persist a validated camera UID and return whether it changed.

        The UID remains out of dashboard snapshots and logs. It is stored only
        so the relay can re-register a known camera bridge mapping after a
        restart without waiting for another feeder boot event.
        """
        now = time.time()
        with self._lock:
            try:
                previous = self._connection.execute(
                    "SELECT uid FROM camera_uids WHERE device_id = ?", (device_id,)
                ).fetchone()
                with self._connection:
                    self._connection.execute(
                        "INSERT INTO camera_uids (device_id, uid, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(device_id) DO UPDATE SET uid = excluded.uid, "
                        "updated_at = excluded.updated_at",
                        (device_id, uid, now),
                    )
            except sqlite3.Error:
                _LOGGER.exception("Failed to record camera UID for %s", device_id)
                raise
        return previous is None or previous[0] != uid

    def update_reported(self, device_id: str, values: dict[str, Any]) -> None:
        """Record physical facts the device reported about itself."""
        self._update_kv("device_reported", device_id, values)

    def update_desired(self, device_id: str, values: dict[str, Any]) -> None:
        """Record settings the cloud last pushed for this device."""
        self._update_kv("cloud_desired", device_id, values)
        # A cloud push and a local feeder ACK have different authorities. Do
        # not erase the ACK merely because an older cloud snapshot was
        # delivered after it; the dashboard can show the divergence instead.
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

    def update_cloud_schedule_plans(self, device_id: str, plans: list[dict[str, Any]]) -> None:
        """Merge a complete cloud schedule snapshot without erasing local IDs.

        Positive cloud IDs are replaced as a set. Negative local IDs remain
        independent, because PETLIBRO does not create them in its own API.
        """
        now = time.time()
        valid_plans = [plan for plan in plans if isinstance(plan.get("planId"), int)]
        cloud_ids = [int(plan["planId"]) for plan in valid_plans if int(plan["planId"]) > 0]
        with self._lock:
            try:
                with self._connection:
                    if cloud_ids:
                        placeholders = ",".join("?" for _ in cloud_ids)
                        self._connection.execute(
                            "DELETE FROM schedule_plans WHERE device_id = ? AND plan_id > 0 "
                            f"AND plan_id NOT IN ({placeholders})",  # noqa: S608 - generated placeholders
                            (device_id, *cloud_ids),
                        )
                    else:
                        self._connection.execute(
                            "DELETE FROM schedule_plans WHERE device_id = ? AND plan_id > 0",
                            (device_id,),
                        )
                    self._connection.executemany(
                        "INSERT INTO schedule_plans (device_id, plan_id, plan_json, source, updated_at) "
                        "VALUES (?, ?, ?, 'cloud', ?) "
                        "ON CONFLICT(device_id, plan_id) DO UPDATE SET "
                        "plan_json = excluded.plan_json, source = excluded.source, "
                        "updated_at = excluded.updated_at",
                        [
                            (device_id, int(plan["planId"]), json.dumps(plan), now)
                            for plan in valid_plans
                            if int(plan["planId"]) > 0
                        ],
                    )
            except sqlite3.Error:
                _LOGGER.exception("Failed to update cloud schedule plans for %s", device_id)
                raise

    def replace_local_schedule_plans(self, device_id: str, plans: list[dict[str, Any]]) -> None:
        """Persist a feeder-confirmed local schedule snapshot.

        A local edit is applied as one complete snapshot to the feeder.  Its
        ownership is nevertheless derived from the plan ID, rather than from
        the actor that made the edit: negative IDs remain local and positive
        IDs remain cloud-owned.  A later cloud snapshot can therefore replace
        only the positive entries without resurrecting or deleting local ones.
        """
        now = time.time()
        persisted_plans = [
            plan
            for plan in plans
            if isinstance(plan.get("planId"), int) and int(plan["planId"]) != 0
        ]
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute("DELETE FROM schedule_plans WHERE device_id = ?", (device_id,))
                    self._connection.executemany(
                        "INSERT INTO schedule_plans (device_id, plan_id, plan_json, source, updated_at) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(device_id, plan_id) DO UPDATE SET "
                        "plan_json = excluded.plan_json, source = excluded.source, "
                        "updated_at = excluded.updated_at",
                        [
                            (
                                device_id,
                                int(plan["planId"]),
                                json.dumps(plan),
                                "local" if int(plan["planId"]) < 0 else "cloud",
                                now,
                            )
                            for plan in persisted_plans
                        ],
                    )
            except sqlite3.Error:
                _LOGGER.exception("Failed to update local schedule plans for %s", device_id)
                raise

    def get_schedule_plans(self, device_id: str) -> list[SchedulePlan]:
        """Return the editable schedule, isolated by device and plan ID."""
        with self._lock:
            try:
                rows = self._connection.execute(
                    "SELECT plan_json, source, updated_at FROM schedule_plans "
                    "WHERE device_id = ? ORDER BY plan_id",
                    (device_id,),
                ).fetchall()
            except sqlite3.Error:
                _LOGGER.exception("Failed to read schedule plans for %s", device_id)
                raise
        return [
            SchedulePlan(plan=json.loads(plan_json), source=source, updated_at=float(updated_at))
            for plan_json, source, updated_at in rows
        ]

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

    def get_camera_uid(self, device_id: str) -> CameraUID | None:
        """Return one stored camera UID for internal bridge registration only."""
        with self._lock:
            row = self._connection.execute(
                "SELECT uid, updated_at FROM camera_uids WHERE device_id = ?", (device_id,)
            ).fetchone()
        if row is None:
            return None
        return CameraUID(device_id=device_id, uid=str(row[0]), updated_at=float(row[1]))

    def get_camera_uids(self) -> list[CameraUID]:
        """Return stored UIDs for relay-to-bridge startup reconciliation only."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT device_id, uid, updated_at FROM camera_uids ORDER BY device_id"
            ).fetchall()
        return [
            CameraUID(device_id=str(device_id), uid=str(uid), updated_at=float(updated_at))
            for device_id, uid, updated_at in rows
        ]

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
            "schedule_plans": [
                {"plan": entry.plan, "source": entry.source, "updated_at": entry.updated_at}
                for entry in self.get_schedule_plans(device_id)
            ],
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
        schedule_plans = self.get_schedule_plans(device_id)
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
            "schedule_plans": [
                {"plan": entry.plan, "source": entry.source, "updated_at": entry.updated_at}
                for entry in schedule_plans
            ],
            "raw_messages": [
                {
                    "topic": topic,
                    "cmd": command,
                    "payload": _decode_raw_payload(payload, command),
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


def _decode_raw_payload(payload: bytes, command: str | None = None) -> Any:
    """Decode raw traffic while keeping learned camera UIDs internal to the relay."""
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")
    if command == "DEVICE_START_EVENT" and isinstance(decoded, dict) and "uuid" in decoded:
        return {**decoded, "uuid": "<redacted>"}
    return decoded
