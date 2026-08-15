"""Contract coverage for the React-only dashboard HTTP surface."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import RelayConfigFactory

from petlibro_relay.camera import CameraStatus, WebRtcExchange
from petlibro_relay.device_context import LOCAL_TO_UPSTREAM
from petlibro_relay.device_manager import DeviceManager
from petlibro_relay.device_presence import DevicePresenceTracker
from petlibro_relay.device_registry import DeviceIdentity, DeviceRegistry
from petlibro_relay.message_queue import MessageQueue
from petlibro_relay.observability.log_buffer import RingBufferLogHandler
from petlibro_relay.observability.sanitizer import REDACTED_VALUE, sanitize_value
from petlibro_relay.observability.telemetry import RelayTelemetry
from petlibro_relay.state_cache import StateCache
from petlibro_relay.state_shadow import StateShadow
from petlibro_relay.web import app as web_app
from petlibro_relay.web.app import create_app
from petlibro_relay.web.context import DashboardContext

DEVICE_A = "TESTDEVICE0000000001"
DEVICE_B = "TESTDEVICE0000000002"
PASSWORD_A = "must-not-appear"
QUEUE_SECRET = "queue-secret-must-not-appear"


class FakeCameraProvider:
    """Provide deterministic, redacted-safe camera status for dashboard tests."""

    def status(self, device_id: str, product_id: str | None) -> CameraStatus:
        """Return a device-scoped camera status without any source URL."""
        return CameraStatus(
            available=True,
            configured=product_id == "PLAF203",
            online=device_id == DEVICE_A,
            stream=f"plaf203_{device_id}",
            webrtc=True,
            go2rtc_reachable=True,
            reason=None,
            bridge_reachable=True,
            bridge_registered=True,
            player_available=True,
        )


def _client_for(
    context: DashboardContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """Build a test client with an isolated compiled React shell."""
    dist = tmp_path / "web-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=\"root\"></div>", encoding="utf-8")
    monkeypatch.setattr(web_app, "FRONTEND_DIST_DIRECTORY", dist)
    return TestClient(create_app(context))


@pytest.fixture
def dashboard(
    make_config: RelayConfigFactory, tmp_path: Path
) -> Iterator[tuple[DashboardContext, RingBufferLogHandler]]:
    """Create a multi-device relay view over throwaway state."""
    config = make_config(web_enabled=True)
    registry = DeviceRegistry(config.device_registry_db_path)
    registry.record(DeviceIdentity(DEVICE_A, "USER12345678", PASSWORD_A))
    registry.record(DeviceIdentity(DEVICE_B, "USERBBBBBBBB", "other-password"))

    queue = MessageQueue(config.queue_db_path, config.max_queue_size)
    queue.enqueue(
        DEVICE_B,
        LOCAL_TO_UPSTREAM,
        f"dl/PLAF203/{DEVICE_B}/device/event/post",
        b'{"cmd":"HEART","password":"' + QUEUE_SECRET.encode() + b'"}',
        0,
    )
    shadow = StateShadow(config.state_shadow_db_path)
    shadow.record_raw(DEVICE_A, f"dl/PLAF203/{DEVICE_A}/device/service/post", b'{"password":"hidden"}', "TEST")
    shadow.update_reported(DEVICE_A, {"rssi": -43, "firmware": "V3.0.30", "tutkP2pRegion": "eu-west"})
    shadow.update_desired(DEVICE_A, {"motionDetectionSwitch": False, "soundSwitch": False})
    shadow.update_feeding_plans(
        DEVICE_A,
        [{"planId": 101, "executionTime": "07:30", "repeatDay": [1, 2, 3], "grainNum": 3}],
        "plan-a",
    )

    telemetry = RelayTelemetry()
    telemetry.device(DEVICE_A).upstream_connect_attempt()
    telemetry.device(DEVICE_A).upstream_online()
    logs = RingBufferLogHandler()
    logs.setFormatter(logging.Formatter("%(message)s"))
    presence = DevicePresenceTracker()
    presence.session_opened(DEVICE_A, "10.3.100.90")
    presence.session_opened(DEVICE_B, "10.3.100.91")
    devices = DeviceManager(
        config, registry, queue, shadow, StateCache(config.state_cache_path), telemetry, presence
    )
    context = DashboardContext(
        config, registry, queue, shadow, telemetry, logs, devices, presence, camera=FakeCameraProvider()
    )
    yield context, logs
    queue.close()
    registry.close()
    shadow.close()


def test_react_shell_serves_canonical_routes_and_redirects_legacy_aliases(
    dashboard: tuple[DashboardContext, RingBufferLogHandler],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The one shell supports direct React routes and preserves harmless bookmarks."""
    context, _ = dashboard
    client = _client_for(context, tmp_path, monkeypatch)

    for path in ("/", "/settings", f"/devices/{DEVICE_A}", f"/devices/{DEVICE_A}/camera"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.text == "<div id=\"root\"></div>"
    for path in ("/devices", "/cloud", "/queues", "/state", "/ntp", "/logs", "/system"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 308
        assert response.headers["location"] == "/"
    assert client.get(f"/devices/{DEVICE_A}?ui=legacy").status_code == 200
    assert client.get("/api/does-not-exist").status_code == 404


def test_public_api_exposes_only_react_projections(
    dashboard: tuple[DashboardContext, RingBufferLogHandler],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Raw legacy diagnostics are unavailable, including queue payloads and logs."""
    context, _ = dashboard
    client = _client_for(context, tmp_path, monkeypatch)

    for path in (
        "/api/status",
        "/api/cloud",
        "/api/devices",
        f"/api/devices/{DEVICE_A}",
        f"/api/queues?device_id={DEVICE_A}",
        f"/api/state?device_id={DEVICE_A}",
        f"/api/ntp?device_id={DEVICE_A}",
        "/api/logs",
        "/api/logs/stream",
        "/api/system",
    ):
        assert client.get(path).status_code == 404
    for path in ("/api/home", f"/api/devices/{DEVICE_A}/daily", f"/api/devices/{DEVICE_A}/advanced", f"/api/devices/{DEVICE_A}/camera"):
        assert client.get(path).status_code == 200


def test_daily_and_advanced_projections_keep_secrets_and_raw_data_out(
    dashboard: tuple[DashboardContext, RingBufferLogHandler],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every user-facing response is an allowlisted projection rather than raw state."""
    context, logs = dashboard
    logs.emit(logging.makeLogRecord({"msg": f"password={PASSWORD_A}", "levelno": logging.INFO, "levelname": "INFO"}))
    client = _client_for(context, tmp_path, monkeypatch)

    daily = client.get(f"/api/devices/{DEVICE_A}/daily").json()
    advanced = client.get(f"/api/devices/{DEVICE_A}/advanced").json()
    daily_serialized = json.dumps(daily)
    advanced_serialized = json.dumps(advanced)

    assert set(daily) == {"device", "state", "controls", "camera", "activity"}
    assert "camera" not in daily["device"]
    for forbidden in ("ip", "mac", "username", "client_id", "firmware", "raw_messages"):
        assert f'"{forbidden}"' not in daily_serialized
    assert PASSWORD_A not in daily_serialized
    assert QUEUE_SECRET not in daily_serialized
    assert set(advanced) == {"device", "connectivity", "camera", "relay", "state_summary", "logs"}
    for forbidden in (PASSWORD_A, QUEUE_SECRET, "tutkP2pRegion", "raw_messages", "password"):
        assert forbidden not in advanced_serialized
    assert sanitize_value({"deviceUuid": "uuid", "apiKey": "key"}) == {
        "deviceUuid": REDACTED_VALUE,
        "apiKey": REDACTED_VALUE,
    }


def test_health_and_camera_status_remain_device_scoped(
    dashboard: tuple[DashboardContext, RingBufferLogHandler],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Health ignores cloud availability while camera data excludes stream sources."""
    context, _ = dashboard
    client = _client_for(context, tmp_path, monkeypatch)

    health = client.get("/healthz").json()
    camera_a = client.get(f"/api/devices/{DEVICE_A}/camera").json()
    camera_b = client.get(f"/api/devices/{DEVICE_B}/camera").json()

    assert health["healthy"] is True
    assert health["devices_known"] == 2
    assert camera_a["online"] is True
    assert camera_b["online"] is False
    assert "source" not in camera_a
    assert "password" not in camera_a


def test_camera_webrtc_proxy_accepts_only_scoped_sdp(
    dashboard: tuple[DashboardContext, RingBufferLogHandler],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The browser cannot select a source, stream, or arbitrary WHEP payload."""
    context, _ = dashboard
    calls: list[tuple[str, str, bytes]] = []

    def exchange(device_id: str, viewer_id: str, offer: bytes) -> WebRtcExchange:
        calls.append((device_id, viewer_id, offer))
        return WebRtcExchange(b"v=0\r\na=answer\r\n", "a" * 32)

    monkeypatch.setattr(context, "exchange_camera_webrtc", exchange)
    client = _client_for(context, tmp_path, monkeypatch)
    response = client.post(
        f"/api/devices/{DEVICE_A}/camera/webrtc",
        content=b"v=0\r\na=offer\r\n",
        headers={"Content-Type": "application/sdp", "X-Relay-Viewer-ID": "a" * 32},
    )

    assert response.status_code == 201
    assert response.content == b"v=0\r\na=answer\r\n"
    assert calls == [(DEVICE_A, "a" * 32, b"v=0\r\na=offer\r\n")]
    assert client.post(f"/api/devices/{DEVICE_A}/camera/webrtc", json={"src": "bad"}).status_code == 415


def test_dashboard_exposes_only_narrow_confirmed_control_write_routes(
    dashboard: tuple[DashboardContext, RingBufferLogHandler],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No generic MQTT or arbitrary-control write endpoint can reach HTTP."""
    context, _ = dashboard
    app = _client_for(context, tmp_path, monkeypatch).app
    write_routes = {
        (route.path, tuple(sorted(route.methods or set())))
        for route in app.routes
        if getattr(route, "methods", None) is not None
        and not route.methods <= {"GET", "HEAD"}
    }
    assert ("/api/devices/{device_id}/controls", ("PATCH",)) not in write_routes
    assert all("mqtt/publish" not in path for path, _ in write_routes)
    assert ("/api/devices/{device_id}/dispense", ("POST",)) in write_routes
    assert ("/api/devices/{device_id}/camera/webrtc", ("POST",)) in write_routes
