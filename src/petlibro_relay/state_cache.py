"""Thread-safe on-disk cache of the last known payload per MQTT topic.

This is distinct from `MessageQueue`: the queue guarantees *delivery* of every
message that passed through the relay, while `StateCache` only ever keeps the
*latest* payload per topic, so the feeder's last known settings and feeding
plan remain readable even across relay restarts or extended cloud outages.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)


class StateCache:
    """Persists the last payload seen on each MQTT topic to a JSON file."""

    def __init__(self, path: str) -> None:
        """Initialize the cache and load any existing data from disk.

        Args:
            path: Filesystem path to the JSON cache file.
        """
        self._path = Path(path)
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            with self._path.open("r", encoding="utf-8") as cache_file:
                loaded: dict[str, dict[str, Any]] = json.load(cache_file)
                return loaded
        except (json.JSONDecodeError, OSError) as error:
            _LOGGER.warning("Could not read state cache at %s, starting empty: %s", self._path, error)
            return {}

    def update(self, topic: str, payload: bytes) -> None:
        """Record the latest payload received for a topic.

        Args:
            topic: MQTT topic the payload was received on.
            payload: Raw message payload.
        """
        decoded_payload: Any
        try:
            decoded_payload = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            decoded_payload = payload.decode("utf-8", errors="replace")

        with self._lock:
            self._entries[topic] = {"payload": decoded_payload, "received_at": time.time()}
            self._flush_locked()

    def _flush_locked(self) -> None:
        """Atomically write the cache to disk. Must be called while holding `self._lock`."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(
                "w", dir=self._path.parent, delete=False, encoding="utf-8"
            ) as tmp_file:
                json.dump(self._entries, tmp_file, indent=2)
                tmp_path = tmp_file.name
            os.replace(tmp_path, self._path)
        except OSError:
            _LOGGER.exception("Failed to persist state cache to %s", self._path)
            raise

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a copy of all cached entries."""
        with self._lock:
            return dict(self._entries)
