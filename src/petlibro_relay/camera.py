"""Read-only go2rtc camera status for the PLAF203 dashboard POC.

The relay deliberately does not construct a TUTK source here. go2rtc v1.9.14
contains a generic TUTK transport, but no PLAF203 producer or source dialect.
This module only reports the state of a deterministically named stream.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Go2RtcSettings

PLAF203_PRODUCT_ID = "PLAF203"
GO2RTC_STREAMS_PATH = "/api/streams"
CACHE_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class CameraStatus:
    """Safe, read-only status exposed to the dashboard."""

    available: bool
    configured: bool
    online: bool
    stream: str
    webrtc: bool
    go2rtc_reachable: bool
    reason: str | None

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-ready status that contains no source or credential."""
        return asdict(self)


class CameraStatusProvider(Protocol):
    """Provide a camera status without coupling dashboard code to HTTP."""

    def status(self, device_id: str, product_id: str | None) -> CameraStatus:
        """Return the safe status for one device."""


class Go2RtcCameraClient:
    """Query the constrained go2rtc stream-list API with a short cache."""

    def __init__(self, settings: Go2RtcSettings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, CameraStatus]] = {}

    def status(self, device_id: str, product_id: str | None) -> CameraStatus:
        """Report a PLAF203 stream without returning its source URL.

        A stream can exist because an operator configured go2rtc separately,
        but this relay will not mark it playable: no PLAF203 TUTK AV dialect
        is implemented or confirmed in go2rtc v1.9.14.
        """
        stream_name = stream_name_for_device(device_id)
        if product_id != PLAF203_PRODUCT_ID:
            return CameraStatus(False, False, False, stream_name, False, False, "unsupported_product")
        if not self._settings.enabled:
            return CameraStatus(False, False, False, stream_name, False, False, "go2rtc_disabled")

        cached = self._cached(device_id)
        if cached is not None:
            return cached
        status = self._fetch_status(stream_name)
        with self._lock:
            self._cache[device_id] = (time.monotonic(), status)
        return status

    def _cached(self, device_id: str) -> CameraStatus | None:
        with self._lock:
            item = self._cache.get(device_id)
        if item is None or time.monotonic() - item[0] >= CACHE_SECONDS:
            return None
        return item[1]

    def _fetch_status(self, stream_name: str) -> CameraStatus:
        try:
            request = Request(self._streams_url(), method="GET")
            with urlopen(request, timeout=self._settings.timeout_seconds) as response:  # noqa: S310
                payload = json.load(response)
        except (HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError):
            return CameraStatus(False, False, False, stream_name, False, False, "go2rtc_unreachable")

        streams = cast(dict[str, Any], payload) if isinstance(payload, dict) else {}
        stream = streams.get(stream_name)
        configured = isinstance(stream, dict)
        online = configured and bool(cast(dict[str, Any], stream).get("producers"))
        return CameraStatus(
            available=False,
            configured=configured,
            online=online,
            stream=stream_name,
            webrtc=False,
            go2rtc_reachable=True,
            reason="plaf203_tutk_unsupported",
        )

    def _streams_url(self) -> str:
        return f"http://{self._settings.host}:{self._settings.port}{GO2RTC_STREAMS_PATH}"


def stream_name_for_device(device_id: str) -> str:
    """Create the deterministic per-device go2rtc stream name."""
    return f"plaf203_{device_id}"
