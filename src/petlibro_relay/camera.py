"""Safe integration points for the optional PLAF203 camera sidecars.

The relay learns a camera UID from a feeder-owned MQTT event, stores it in the
state shadow, and can register it with our local camera bridge. The bridge is
the only component allowed to attempt camera transport work. This module does
not manufacture a TUTK session, source URL, or credentials.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import CameraBridgeSettings, Go2RtcSettings
from .camera_uid import is_camera_uid

_LOGGER = logging.getLogger(__name__)

PLAF203_PRODUCT_ID = "PLAF203"
GO2RTC_STREAMS_PATH = "/api/streams"
CAMERA_BRIDGE_DEVICES_PATH = "/devices"
CAMERA_BRIDGE_HEALTH_PATH = "/healthz"
CACHE_SECONDS = 2.0
REGISTRATION_QUEUE_MAXSIZE = 128
REGISTRAR_POLL_TIMEOUT_SECONDS = 0.1

CameraBridgeMapping = tuple[str, str, str | None]


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
    bridge_reachable: bool = False
    bridge_registered: bool = False
    uid_learned: bool = False

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-ready status that contains no source or credential."""
        return asdict(self)


class CameraStatusProvider(Protocol):
    """Provide a camera status without coupling dashboard code to HTTP."""

    def status(self, device_id: str, product_id: str | None) -> CameraStatus:
        """Return the safe status for one device."""


class CameraBridgeRegistrationClient(Protocol):
    """Register a feeder UID with the internal camera bridge."""

    def register(self, device_id: str, uid: str, feeder_ip: str | None) -> bool:
        """Register one UID and return whether the bridge accepted it."""


class CameraBridgeReconciliationClient(CameraBridgeRegistrationClient, Protocol):
    """Read the non-sensitive runtime camera-bridge registry."""

    def health(self) -> bool:
        """Return whether the camera bridge is reachable and healthy."""

    def registrations(self) -> dict[str, str | None] | None:
        """Return device IDs and registered feeder IPs, or None when unavailable."""


class CameraBridgeClient:
    """Constrained client for the local camera bridge registration API."""

    def __init__(self, settings: CameraBridgeSettings) -> None:
        self._settings = settings

    def register(self, device_id: str, uid: str, feeder_ip: str | None) -> bool:
        """PUT one learned UID without exposing it in diagnostics.

        This operation is only called from the registrar worker, never from an
        MQTT callback thread.
        """
        if not self._settings.enabled:
            return False
        payload_fields: dict[str, str] = {"uid": uid}
        if feeder_ip is not None:
            payload_fields["ip"] = feeder_ip
        payload = json.dumps(payload_fields).encode()
        request = Request(
            f"{self._base_url()}{CAMERA_BRIDGE_DEVICES_PATH}/{device_id}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        try:
            with urlopen(request, timeout=self._settings.timeout_seconds) as response:  # noqa: S310
                status = getattr(response, "status", 200)
                return isinstance(status, int) and 200 <= status < 300
        except (HTTPError, URLError, OSError, TimeoutError):
            return False

    def status(self, device_id: str) -> tuple[bool, bool]:
        """Return `(reachable, registered)` without returning a UID."""
        registrations = self.registrations()
        if registrations is None:
            return False, False
        return True, device_id in registrations

    def health(self) -> bool:
        """Check the sidecar health endpoint without reading sensitive state."""
        if not self._settings.enabled:
            return False
        request = Request(f"{self._base_url()}{CAMERA_BRIDGE_HEALTH_PATH}", method="GET")
        try:
            with urlopen(request, timeout=self._settings.timeout_seconds) as response:  # noqa: S310
                status = getattr(response, "status", 200)
                return isinstance(status, int) and 200 <= status < 300
        except (HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError):
            return False

    def registrations(self) -> dict[str, str | None] | None:
        """List current bridge registrations without exposing UIDs to the relay."""
        if not self._settings.enabled:
            return None
        request = Request(f"{self._base_url()}{CAMERA_BRIDGE_DEVICES_PATH}", method="GET")
        try:
            with urlopen(request, timeout=self._settings.timeout_seconds) as response:  # noqa: S310
                payload = json.load(response)
        except (HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError):
            return None
        devices = payload.get("devices") if isinstance(payload, dict) else None
        if not isinstance(devices, list):
            return None
        registrations: dict[str, str | None] = {}
        for device in devices:
            if not isinstance(device, dict):
                continue
            device_id = device.get("device_id")
            if not isinstance(device_id, str) or not device_id:
                continue
            feeder_ip = device.get("ip")
            registrations[device_id] = feeder_ip if isinstance(feeder_ip, str) else None
        return registrations

    def _base_url(self) -> str:
        if self._settings.url is not None:
            return self._settings.url.rstrip("/")
        return f"http://{self._settings.host}:{self._settings.port}"


class CameraBridgeRegistrar:
    """Queue bridge registration work away from MQTT callback threads."""

    def __init__(self, settings: CameraBridgeSettings, client: CameraBridgeRegistrationClient) -> None:
        self._settings = settings
        self._client = client
        self._registered: set[CameraBridgeMapping] = set()
        self._in_flight: set[CameraBridgeMapping] = set()
        self._lock = threading.Lock()
        self._pending: queue.Queue[CameraBridgeMapping] = queue.Queue(REGISTRATION_QUEUE_MAXSIZE)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        if settings.enabled:
            self._thread = threading.Thread(target=self._run, name="camera-bridge-register")
            self._thread.start()

    def register(
        self, device_id: str, uid: str, feeder_ip: str | None = None, *, force: bool = False
    ) -> bool:
        """Schedule idempotent registration after the UID is safely persisted."""
        if self._stop_event.is_set() or not self._settings.enabled or not is_camera_uid(uid):
            return False
        registration = (device_id, uid, feeder_ip)
        with self._lock:
            if registration in self._in_flight or (not force and registration in self._registered):
                return False
            self._in_flight.add(registration)
        try:
            self._pending.put_nowait(registration)
        except queue.Full:
            with self._lock:
                self._in_flight.discard(registration)
            _LOGGER.warning("CAMERA BRIDGE REGISTER queue full device_id=%s", device_id)
            return False
        return True

    def reconcile(self, mappings: Iterable[CameraBridgeMapping], *, force: bool = False) -> int:
        """Schedule persisted UID mappings and return how many were queued."""
        scheduled = 0
        for device_id, uid, feeder_ip in mappings:
            if self.register(device_id, uid, feeder_ip, force=force):
                scheduled += 1
        return scheduled

    def close(self) -> None:
        """Stop the optional registration worker without touching cameras."""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                registration = self._pending.get(timeout=REGISTRAR_POLL_TIMEOUT_SECONDS)
            except queue.Empty:
                continue
            device_id, uid, feeder_ip = registration
            if self._client.register(device_id, uid, feeder_ip):
                with self._lock:
                    updating = any(
                        existing_registration[0] == device_id and existing_registration != registration
                        for existing_registration in self._registered
                    )
                    self._in_flight.discard(registration)
                    self._registered.add(registration)
                operation = "UPDATED" if updating else "REGISTERED"
                _LOGGER.info("CAMERA BRIDGE %s device=%s", operation, device_id)
                continue
            with self._lock:
                self._in_flight.discard(registration)
                self._registered.discard(registration)
            _LOGGER.debug("CAMERA BRIDGE REGISTER device_id=%s result=unavailable", device_id)


class CameraBridgeReconciler:
    """Converge persisted relay mappings into the restartable bridge registry."""

    def __init__(
        self,
        settings: CameraBridgeSettings,
        client: CameraBridgeReconciliationClient,
        registrar: CameraBridgeRegistrar,
        mappings_provider: Callable[[], Iterable[CameraBridgeMapping]],
    ) -> None:
        """Create a non-blocking reconciliation service.

        Args:
            settings: Camera bridge feature flag, HTTP settings, and cadence.
            client: Restricted sidecar health and registration-list client.
            registrar: Existing serialized registration worker.
            mappings_provider: Persistent relay-side camera mapping source.
        """
        self._settings = settings
        self._client = client
        self._registrar = registrar
        self._mappings_provider = mappings_provider
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._available: bool | None = None

    def start(self) -> None:
        """Start background convergence when the optional sidecar is enabled."""
        if not self._settings.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="camera-bridge-reconcile")
        self._thread.start()

    def reconcile_once(self) -> None:
        """Compare persisted mappings with the current bridge registry once."""
        if not self._settings.enabled:
            return
        if not self._client.health():
            self._mark_offline()
            return
        bridge_registry = self._client.registrations()
        if bridge_registry is None:
            self._mark_offline()
            return

        recovered_from_unavailable = self._available is False
        if self._available is not True:
            _LOGGER.info("CAMERA BRIDGE ONLINE")
        self._available = True
        persisted = list(self._mappings_provider())
        missing = [
            mapping
            for mapping in persisted
            if mapping[0] not in bridge_registry or bridge_registry[mapping[0]] != mapping[2]
        ]
        targets = persisted if recovered_from_unavailable else missing
        scheduled = 0
        for device_id, uid, feeder_ip in targets:
            if self._registrar.register(device_id, uid, feeder_ip, force=True):
                scheduled += 1
                _LOGGER.info("CAMERA BRIDGE RECONCILE device=%s action=register", device_id)
        if targets:
            _LOGGER.info(
                "CAMERA BRIDGE RECONCILE persisted=%d registered=%d missing=%d",
                len(persisted),
                len(bridge_registry),
                len(missing),
            )
        else:
            _LOGGER.debug(
                "CAMERA BRIDGE RECONCILE persisted=%d registered=%d missing=0",
                len(persisted),
                len(bridge_registry),
            )
        if scheduled:
            _LOGGER.info("CAMERA BRIDGE RECONCILE complete changed=%d", scheduled)
        else:
            _LOGGER.debug("CAMERA BRIDGE RECONCILE complete changed=0")

    def close(self) -> None:
        """Stop and join the reconciliation worker before dependent shutdown."""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.reconcile_once()
            self._stop_event.wait(self._settings.reconcile_interval_seconds)

    def _mark_offline(self) -> None:
        if self._available is not False:
            _LOGGER.warning("CAMERA BRIDGE OFFLINE")
            _LOGGER.warning("CAMERA BRIDGE RECONCILE FAILED reason=unreachable")
        self._available = False


class CameraStatusService:
    """Combine safe go2rtc and bridge readiness diagnostics for one camera."""

    def __init__(self, go2rtc: Go2RtcSettings, bridge: CameraBridgeSettings) -> None:
        self._go2rtc = Go2RtcCameraClient(go2rtc)
        self._bridge = CameraBridgeClient(bridge)

    def status(self, device_id: str, product_id: str | None) -> CameraStatus:
        """Return sidecar state without exposing source URLs or camera UIDs."""
        status = self._go2rtc.status(device_id, product_id)
        bridge_reachable, bridge_registered = self._bridge.status(device_id)
        return replace(
            status,
            bridge_reachable=bridge_reachable,
            bridge_registered=bridge_registered,
        )


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
