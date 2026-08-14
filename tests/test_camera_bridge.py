"""Tests for safe PLAF203 UID learning and camera-bridge registration."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from petlibro_relay.camera import CameraBridgeRegistrar
from petlibro_relay.config import CameraBridgeSettings
from petlibro_relay.local_responder import Decision, LocalResponder, LocalResponderSettings, UpstreamState
from petlibro_relay.state_shadow import StateShadow

DEVICE_ID = "AF03040302A2B5B2CD60"
CAMERA_UID = "PLAF2030000000000001"
DEVICE_START_TOPIC = f"dl/PLAF203/{DEVICE_ID}/device/event/post"


@pytest.fixture
def shadow(tmp_path: Path) -> Iterator[StateShadow]:
    """Provide a temporary state shadow that is always closed."""
    instance = StateShadow(str(tmp_path / "shadow.sqlite3"))
    yield instance
    instance.close()


class CameraObserver:
    """Record callback registrations without making an HTTP request."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def register(self, device_id: str, uid: str) -> None:
        """Record one non-blocking callback invocation."""
        self.calls.append((device_id, uid))


class FakeBridgeClient:
    """Signal every registration sent by the worker without storing a UID in logs."""

    def __init__(self, accepted: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self.called = threading.Event()
        self._accepted = accepted

    def register(self, device_id: str, uid: str) -> bool:
        """Accept a registration request deterministically."""
        self.calls.append((device_id, uid))
        self.called.set()
        return self._accepted


def test_device_start_uid_is_persisted_and_observed_without_changing_forwarding(
    shadow: StateShadow, caplog: pytest.LogCaptureFixture
) -> None:
    """A valid UID is learned while the event remains a normal forwardable post."""
    observer = CameraObserver()
    responder = LocalResponder(
        LocalResponderSettings(), shadow, handled_msg_id_ttl_seconds=120.0, camera_uid_observer=observer.register
    )
    payload = json.dumps(
        {"cmd": "DEVICE_START_EVENT", "uuid": CAMERA_UID, "softwareVersion": "V3.0.30"}
    ).encode()

    with caplog.at_level(logging.INFO, logger="petlibro_relay.local_responder"):
        action = responder.decide(DEVICE_ID, DEVICE_START_TOPIC, payload, UpstreamState.ONLINE)

    assert action.decision is Decision.CACHE_AND_FORWARD
    stored_uid = shadow.get_camera_uid(DEVICE_ID)
    assert stored_uid is not None
    assert stored_uid.uid == CAMERA_UID
    assert observer.calls == [(DEVICE_ID, CAMERA_UID)]
    raw_messages = shadow.dashboard_snapshot(DEVICE_ID)["raw_messages"]
    assert raw_messages[0]["payload"]["uuid"] == "<redacted>"
    assert "CAMERA UID LEARNED device_id=" + DEVICE_ID in caplog.text
    assert CAMERA_UID not in caplog.text


def test_invalid_device_start_uuid_is_not_stored_or_registered(shadow: StateShadow) -> None:
    """Only the confirmed 20-character UID shape reaches the bridge path."""
    observer = CameraObserver()
    responder = LocalResponder(
        LocalResponderSettings(), shadow, handled_msg_id_ttl_seconds=120.0, camera_uid_observer=observer.register
    )
    payload = json.dumps({"cmd": "DEVICE_START_EVENT", "uuid": "not-a-camera-uid"}).encode()

    responder.decide(DEVICE_ID, DEVICE_START_TOPIC, payload, UpstreamState.ONLINE)

    assert shadow.get_camera_uid(DEVICE_ID) is None
    assert observer.calls == []


def test_registration_worker_coalesces_duplicate_uid_events() -> None:
    """Repeated device-start events produce one idempotent bridge registration."""
    client = FakeBridgeClient()
    registrar = CameraBridgeRegistrar(CameraBridgeSettings(enabled=True), client)
    try:
        registrar.register(DEVICE_ID, CAMERA_UID)
        registrar.register(DEVICE_ID, CAMERA_UID)
        assert client.called.wait(timeout=1.0)
        assert client.calls == [(DEVICE_ID, CAMERA_UID)]
    finally:
        registrar.close()


def test_unavailable_camera_bridge_does_not_affect_uid_registration_callers() -> None:
    """A failed internal registration remains isolated from feeder event handling."""
    client = FakeBridgeClient(accepted=False)
    registrar = CameraBridgeRegistrar(CameraBridgeSettings(enabled=True), client)
    try:
        registrar.register(DEVICE_ID, CAMERA_UID)
        assert client.called.wait(timeout=1.0)
        assert client.calls == [(DEVICE_ID, CAMERA_UID)]
    finally:
        registrar.close()
