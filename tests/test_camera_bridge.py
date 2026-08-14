"""Tests for safe PLAF203 UID learning and camera-bridge registration."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import RelayConfigFactory

from petlibro_relay.camera import CameraBridgeClient, CameraBridgeRegistrar
from petlibro_relay.config import CameraBridgeSettings
from petlibro_relay.device_manager import DeviceManager
from petlibro_relay.device_presence import DevicePresenceTracker
from petlibro_relay.device_registry import DeviceRegistry
from petlibro_relay.local_responder import Decision, LocalResponder, LocalResponderSettings, UpstreamState
from petlibro_relay.message_queue import MessageQueue
from petlibro_relay.observability.telemetry import RelayTelemetry
from petlibro_relay.state_cache import StateCache
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
        self.calls: list[tuple[str, str, str | None]] = []
        self.called = threading.Event()
        self._accepted = accepted

    def register(self, device_id: str, uid: str, feeder_ip: str | None) -> bool:
        """Accept a registration request deterministically."""
        self.calls.append((device_id, uid, feeder_ip))
        self.called.set()
        return self._accepted


class FakeHttpResponse:
    """Minimal successful response for the registration client's PUT request."""

    status = 200

    def __enter__(self) -> "FakeHttpResponse":
        """Enter the in-memory response context."""
        return self

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None:
        """Close nothing because the response has no resources."""


def test_camera_bridge_client_sends_uid_and_known_feeder_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The internal bridge registration includes the local TCP peer IPv4 address."""
    captured: dict[str, Any] = {}

    def capture_request(request: object, timeout: float) -> FakeHttpResponse:
        """Capture the constrained internal PUT without opening a socket."""
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeHttpResponse()

    monkeypatch.setattr("petlibro_relay.camera.urlopen", capture_request)
    client = CameraBridgeClient(CameraBridgeSettings(enabled=True, host="camera-bridge", port=8081))

    assert client.register(DEVICE_ID, CAMERA_UID, "10.3.100.90") is True
    request = captured["request"]
    assert getattr(request, "method") == "PUT"
    assert getattr(request, "full_url").endswith(f"/devices/{DEVICE_ID}")
    assert json.loads(getattr(request, "data")) == {"uid": CAMERA_UID, "ip": "10.3.100.90"}


def test_camera_uid_ip_is_migrated_and_persisted(tmp_path: Path) -> None:
    """An existing UID-only shadow gains the optional feeder IPv4 column safely."""
    database_path = tmp_path / "shadow.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "CREATE TABLE camera_uids (device_id TEXT PRIMARY KEY, uid TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        connection.execute(
            "INSERT INTO camera_uids (device_id, uid, updated_at) VALUES (?, ?, ?)",
            (DEVICE_ID, CAMERA_UID, 1.0),
        )
        connection.commit()
    finally:
        connection.close()

    shadow = StateShadow(str(database_path))
    try:
        original = shadow.get_camera_uid(DEVICE_ID)
        assert original is not None
        assert original.feeder_ip is None
        assert shadow.record_camera_uid(DEVICE_ID, CAMERA_UID, "10.3.100.90") is True
        updated = shadow.get_camera_uid(DEVICE_ID)
        assert updated is not None
        assert updated.feeder_ip == "10.3.100.90"
    finally:
        shadow.close()


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
        assert client.calls == [(DEVICE_ID, CAMERA_UID, None)]
    finally:
        registrar.close()


def test_registration_worker_sends_ip_updates_but_coalesces_identical_mapping() -> None:
    """A feeder address change refreshes the bridge without duplicating one mapping."""
    client = FakeBridgeClient()
    registrar = CameraBridgeRegistrar(CameraBridgeSettings(enabled=True), client)
    try:
        registrar.register(DEVICE_ID, CAMERA_UID, "10.3.100.90")
        registrar.register(DEVICE_ID, CAMERA_UID, "10.3.100.90")
        registrar.register(DEVICE_ID, CAMERA_UID, "10.3.100.91")
        deadline = time.monotonic() + 1.0
        while len(client.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.calls == [
            (DEVICE_ID, CAMERA_UID, "10.3.100.90"),
            (DEVICE_ID, CAMERA_UID, "10.3.100.91"),
        ]
    finally:
        registrar.close()


def test_startup_reconciliation_includes_persisted_feeder_ip() -> None:
    """A relay restart re-registers the UID with its last known local address."""
    client = FakeBridgeClient()
    registrar = CameraBridgeRegistrar(CameraBridgeSettings(enabled=True), client)
    try:
        registrar.reconcile([(DEVICE_ID, CAMERA_UID, "10.3.100.90")])
        assert client.called.wait(timeout=1.0)
        assert client.calls == [(DEVICE_ID, CAMERA_UID, "10.3.100.90")]
    finally:
        registrar.close()


def test_device_start_event_registers_uid_with_current_local_peer_ip(
    make_config: RelayConfigFactory,
) -> None:
    """A UID learned after CONNECT is registered with the feeder's observed IPv4 address."""
    config = make_config(camera_bridge=CameraBridgeSettings(enabled=True))
    registry = DeviceRegistry(config.device_registry_db_path)
    queue = MessageQueue(config.queue_db_path, config.max_queue_size)
    shadow = StateShadow(config.state_shadow_db_path)
    state_cache = StateCache(config.state_cache_path)
    presence = DevicePresenceTracker()
    presence.session_opened(DEVICE_ID, "10.3.100.90")
    client = FakeBridgeClient()
    registrar = CameraBridgeRegistrar(CameraBridgeSettings(enabled=True), client)
    manager = DeviceManager(
        config,
        registry,
        queue,
        shadow,
        state_cache,
        RelayTelemetry(),
        presence,
        camera_registrar=registrar,
    )
    try:
        payload = json.dumps({"cmd": "DEVICE_START_EVENT", "uuid": CAMERA_UID}).encode()
        action = manager._build_responder().decide(
            DEVICE_ID, DEVICE_START_TOPIC, payload, UpstreamState.ONLINE
        )

        assert action.decision is Decision.CACHE_AND_FORWARD
        assert client.called.wait(timeout=1.0)
        assert client.calls == [(DEVICE_ID, CAMERA_UID, "10.3.100.90")]
        stored = shadow.get_camera_uid(DEVICE_ID)
        assert stored is not None
        assert stored.feeder_ip == "10.3.100.90"
    finally:
        registrar.close()
        manager.stop()
        registry.close()
        queue.close()
        shadow.close()


def test_peer_ip_change_refreshes_persisted_bridge_mapping(
    make_config: RelayConfigFactory,
) -> None:
    """A later TCP session updates the bridge mapping without touching a camera session."""
    config = make_config(camera_bridge=CameraBridgeSettings(enabled=True))
    registry = DeviceRegistry(config.device_registry_db_path)
    queue = MessageQueue(config.queue_db_path, config.max_queue_size)
    shadow = StateShadow(config.state_shadow_db_path)
    state_cache = StateCache(config.state_cache_path)
    client = FakeBridgeClient()
    registrar = CameraBridgeRegistrar(CameraBridgeSettings(enabled=True), client)
    manager = DeviceManager(
        config,
        registry,
        queue,
        shadow,
        state_cache,
        RelayTelemetry(),
        DevicePresenceTracker(),
        camera_registrar=registrar,
    )
    try:
        shadow.record_camera_uid(DEVICE_ID, CAMERA_UID)
        manager.record_camera_peer_address(DEVICE_ID, "10.3.100.90")
        manager.record_camera_peer_address(DEVICE_ID, "10.3.100.91")
        deadline = time.monotonic() + 1.0
        while len(client.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.calls == [
            (DEVICE_ID, CAMERA_UID, "10.3.100.90"),
            (DEVICE_ID, CAMERA_UID, "10.3.100.91"),
        ]
        stored = shadow.get_camera_uid(DEVICE_ID)
        assert stored is not None
        assert stored.feeder_ip == "10.3.100.91"
    finally:
        registrar.close()
        manager.stop()
        registry.close()
        queue.close()
        shadow.close()


def test_unavailable_camera_bridge_does_not_affect_uid_registration_callers() -> None:
    """A failed internal registration remains isolated from feeder event handling."""
    client = FakeBridgeClient(accepted=False)
    registrar = CameraBridgeRegistrar(CameraBridgeSettings(enabled=True), client)
    try:
        registrar.register(DEVICE_ID, CAMERA_UID)
        assert client.called.wait(timeout=1.0)
        assert client.calls == [(DEVICE_ID, CAMERA_UID, None)]
    finally:
        registrar.close()
