"""Tracks which devices are actually connected to the local broker right now.

This is the only source of truth for local presence, and it is fed exclusively
by sessions the `CredentialCaptureProxy` observed opening and closing. A known
identity is not a connected device: the registry keeps entries for 72h, and a
manually seeded one has never connected at all.

Presence drives more than the dashboard. A device that is not here should not
have the relay holding a cloud session open in its name, so `DeviceManager`
uses this to decide whether each device's upstream client should be running.

A session that ended moments ago still counts as present, to ride out the gap
between a feeder dropping its link and reconnecting; past that grace the
device is offline.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

# How long after its last local session a device is still treated as present.
# Sized to comfortably cover a feeder's reconnect (seconds) without keeping a
# powered-off device looking alive.
LOCAL_PRESENCE_GRACE_SECONDS = 90.0


class LocalPresence(StrEnum):
    """Whether a device currently holds - or just held - a local session."""

    ONLINE = "LOCAL_ONLINE"
    OFFLINE = "LOCAL_OFFLINE"


@dataclass(frozen=True, slots=True)
class PresenceRecord:
    """What is known about one device's local connectivity."""

    device_id: str
    connected: bool
    last_opened_at: float | None
    last_closed_at: float | None
    peer_address: str | None
    connection_generation: int


class DevicePresenceTracker:
    """Records local session lifecycle per device, with a reconnect grace."""

    def __init__(
        self,
        grace_seconds: float = LOCAL_PRESENCE_GRACE_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialize the tracker.

        Args:
            grace_seconds: How long a just-ended session still counts as
                present.
            clock: Time source, injectable for tests.
        """
        self._grace_seconds = grace_seconds
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._records: dict[str, PresenceRecord] = {}

    @property
    def grace_seconds(self) -> float:
        """Return the reconnect grace window."""
        return self._grace_seconds

    def session_opened(self, device_id: str, peer_address: str | None = None) -> None:
        """Record that a device has connected to the local broker."""
        with self._lock:
            existing = self._records.get(device_id)
            self._records[device_id] = PresenceRecord(
                device_id=device_id,
                connected=True,
                last_opened_at=self._clock(),
                last_closed_at=existing.last_closed_at if existing is not None else None,
                peer_address=peer_address
                or (existing.peer_address if existing is not None else None),
                connection_generation=(existing.connection_generation if existing is not None else 0) + 1,
            )

    def session_closed(self, device_id: str) -> None:
        """Record that a device's last local session has ended."""
        with self._lock:
            existing = self._records.get(device_id)
            self._records[device_id] = PresenceRecord(
                device_id=device_id,
                connected=False,
                last_opened_at=existing.last_opened_at if existing is not None else None,
                last_closed_at=self._clock(),
                peer_address=existing.peer_address if existing is not None else None,
                connection_generation=existing.connection_generation if existing is not None else 0,
            )

    def state(self, device_id: str) -> LocalPresence:
        """Return whether a device counts as locally present."""
        with self._lock:
            record = self._records.get(device_id)
            now = self._clock()
        if record is None:
            # Never observed connecting, so never present - regardless of how
            # recently its identity was learned or seeded.
            return LocalPresence.OFFLINE
        if record.connected:
            return LocalPresence.ONLINE
        if (
            record.last_closed_at is not None
            and now - record.last_closed_at <= self._grace_seconds
        ):
            return LocalPresence.ONLINE
        return LocalPresence.OFFLINE

    def is_online(self, device_id: str) -> bool:
        """Return True while a device is present or inside its reconnect grace."""
        return self.state(device_id) is LocalPresence.ONLINE

    def record(self, device_id: str) -> PresenceRecord | None:
        """Return the raw record for a device, if it has ever connected."""
        with self._lock:
            return self._records.get(device_id)

    def online_device_ids(self) -> set[str]:
        """Return every device currently counted as present."""
        with self._lock:
            device_ids = list(self._records)
        return {device_id for device_id in device_ids if self.is_online(device_id)}
