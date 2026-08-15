"""Read-only API and SSE coverage for the multi-device observability dashboard.

Two things are being defended here: the dashboard stays strictly read-only,
and it never exposes a device's credentials. The multi-device tests also check
that one device's metrics and state are not attributed to another.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import RelayConfigFactory

from petlibro_relay.device_context import LOCAL_TO_UPSTREAM
from petlibro_relay.camera import CameraStatus, WebRtcExchange
from petlibro_relay.device_manager import DeviceManager
from petlibro_relay.device_presence import DevicePresenceTracker
from petlibro_relay.device_registry import DeviceIdentity, DeviceRegistry
from petlibro_relay.message_queue import MessageQueue
from petlibro_relay.observability.log_buffer import RingBufferLogHandler
from petlibro_relay.observability.telemetry import RelayTelemetry
from petlibro_relay.state_cache import StateCache
from petlibro_relay.state_shadow import StateShadow
from petlibro_relay.web import app as web_app
from petlibro_relay.web.app import _stream_logs, create_app
from petlibro_relay.web.context import DashboardContext
from petlibro_relay.web.static import DASHBOARD_HTML
from petlibro_relay.web.user_interface import DASHBOARD_JAVASCRIPT

DEVICE_A = "TESTDEVICE0000000001"
DEVICE_B = "TESTDEVICE0000000002"
DEVICE_C = "TESTDEVICE0000000003"
USERNAME_A = "USER12345678"
PASSWORD_A = "must-not-appear"
PASSWORD_B = "b-must-not-appear"
PASSWORD_C = "c-must-not-appear"
QUEUE_SECRET = "queue-secret-must-not-appear"
HTML_INJECTION_VALUE = "<script>must-not-run</script>"
DEVICE_A_PLANS = [
    {
        "planId": 101,
        "executionTime": "07:30",
        "repeatDay": [1, 2, 3, 4, 5],
        "grainNum": 3,
        "tutkP2pRegion": "internal-region",
        "credentialHint": "must-not-appear",
        "internalUrl": "http://private.example.invalid",
    }
]
DEVICE_B_PLANS = [
    {
        "planId": 202,
        "executionTime": "19:00",
        "repeatDay": [7],
        "grainNum": 1,
    }
]


class FakeCameraProvider:
    """Provide deterministic camera state without reaching a real sidecar."""

    def status(self, device_id: str, product_id: str | None) -> CameraStatus:
        """Return a safe status scoped to the requested device."""
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


def test_react_shell_preserves_unmigrated_legacy_routes_and_api_404s(
    dashboard: tuple[DashboardContext, RingBufferLogHandler],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Use the legacy UI for unmigrated routes while preserving API 404 semantics."""
    context, _ = dashboard
    dist = tmp_path / "web-dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<div id=\"root\"></div>", encoding="utf-8")
    monkeypatch.setattr(web_app, "FRONTEND_DIST_DIRECTORY", dist)

    client = TestClient(create_app(context, frontend="react"))

    device_route = client.get(f"/devices/{DEVICE_A}/overview", follow_redirects=False)
    assert device_route.status_code == 302
    assert device_route.headers["location"] == f"/devices/{DEVICE_A}?ui=legacy#overview"
    for legacy_path in ("/devices", "/cloud", "/queues", "/state", "/ntp", "/logs", "/system", "/settings"):
        legacy_route = client.get(legacy_path, follow_redirects=False)
        assert legacy_route.status_code == 302
        assert legacy_route.headers["location"] == f"{legacy_path}?ui=legacy"
    for react_path in ("camera", "schedule"):
        react_route = client.get(f"/devices/{DEVICE_A}/{react_path}")
        assert react_route.status_code == 200
        assert react_route.text == "<div id=\"root\"></div>"
    assert client.get(f"/devices/{DEVICE_A}?ui=legacy").text == DASHBOARD_HTML
    assert client.get("/api/does-not-exist").status_code == 404


@pytest.fixture
def dashboard(
    make_config: RelayConfigFactory, tmp_path: Path
) -> Iterator[tuple[DashboardContext, RingBufferLogHandler]]:
    """A dashboard over three devices in deliberately different states.

    A is local-online with a healthy cloud session, B is local-online with the
    cloud down and a backlog, C has never come online.
    """
    config = make_config(web_enabled=True)
    registry = DeviceRegistry(config.device_registry_db_path)
    registry.record(DeviceIdentity(DEVICE_A, USERNAME_A, PASSWORD_A))
    registry.record(DeviceIdentity(DEVICE_B, "USERBBBBBBBB", PASSWORD_B))
    registry.record(DeviceIdentity(DEVICE_C, "USERCCCCCCCC", PASSWORD_C))

    queue = MessageQueue(config.queue_db_path, config.max_queue_size)
    queue.enqueue(
        DEVICE_B,
        LOCAL_TO_UPSTREAM,
        f"dl/PLAF203/{DEVICE_B}/device/event/post",
        b'{"cmd":"HEART","password":"' + QUEUE_SECRET.encode() + b'"}',
        0,
    )

    shadow = StateShadow(config.state_shadow_db_path)
    shadow.record_raw(
        DEVICE_A, f"dl/PLAF203/{DEVICE_A}/device/service/post", b'{"password":"hidden"}', "TEST"
    )
    shadow.record_raw(
        DEVICE_A, f"dl/PLAF203/{DEVICE_A}/device/ntp/post", b'{"cmd":"NTP","ts":1}', "NTP"
    )
    shadow.update_reported(
        DEVICE_A,
        {"rssi": -43, "firmware": "V3.0.30", "tutkP2pRegion": "eu-west"},
    )
    shadow.update_reported(DEVICE_B, {"rssi": -51})
    shadow.update_desired(
        DEVICE_A,
        {
            "motionDetectionSwitch": False,
            "motionDetectionSensitivity": 2,
            "soundDetectionSwitch": True,
            "soundSwitch": False,
            "displayName": HTML_INJECTION_VALUE,
        },
    )
    shadow.update_desired(DEVICE_B, {"soundSwitch": True})
    shadow.update_feeding_plans(DEVICE_A, DEVICE_A_PLANS, "plan-a")
    shadow.update_feeding_plans(DEVICE_B, DEVICE_B_PLANS, "plan-b")

    telemetry = RelayTelemetry()
    telemetry.device(DEVICE_A).upstream_connect_attempt()
    telemetry.device(DEVICE_A).upstream_online()
    telemetry.device(DEVICE_A).increment("ntp_requests")
    telemetry.device(DEVICE_B).upstream_connect_attempt()
    telemetry.device(DEVICE_B).upstream_disconnected("socket reset")

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


@pytest.fixture
def client(dashboard: tuple[DashboardContext, RingBufferLogHandler]) -> TestClient:
    """Build a FastAPI test client without opening a real HTTP listener."""
    context, _ = dashboard
    return TestClient(create_app(context))


@pytest.mark.parametrize(
    "path",
    [
        "/api/status",
        "/api/cloud",
        "/api/devices",
        f"/api/devices/{DEVICE_A}",
        f"/api/devices/{DEVICE_A}/camera",
        f"/api/queues?device_id={DEVICE_A}",
        f"/api/state?device_id={DEVICE_A}",
        f"/api/ntp?device_id={DEVICE_A}",
        "/api/logs",
        "/api/system",
    ],
)
def test_read_only_api_endpoints_return_json(client: TestClient, path: str) -> None:
    """Every documented read-only endpoint returns a valid success payload."""
    response = client.get(path)

    assert response.status_code == 200
    assert isinstance(response.json(), dict)


def test_health_remains_ok_when_petlibro_is_down(client: TestClient) -> None:
    """Cloud failure must not make Docker/Kubernetes mark the local relay unhealthy."""
    payload = client.get("/healthz").json()

    assert payload["healthy"] is True
    assert payload["devices_known"] == 3
    assert payload["upstream_petlibro_online"] == 1, "only A has a live cloud session"


def test_camera_status_is_device_scoped_and_excludes_sensitive_source_data(client: TestClient) -> None:
    """Camera diagnostics expose only safe go2rtc status, never a source URL."""
    camera_a = client.get(f"/api/devices/{DEVICE_A}/camera").json()
    camera_b = client.get(f"/api/devices/{DEVICE_B}/camera").json()

    assert camera_a["stream"] == f"plaf203_{DEVICE_A}"
    assert camera_a["configured"] is True
    assert camera_a["online"] is True
    assert camera_b["stream"] == f"plaf203_{DEVICE_B}"
    assert camera_b["online"] is False
    assert "source" not in camera_a
    assert "password" not in camera_a


def test_camera_webrtc_proxy_is_device_scoped_and_accepts_only_sdp(
    dashboard: tuple[DashboardContext, RingBufferLogHandler], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The player route cannot select a source, stream, or arbitrary payload."""
    context, _ = dashboard
    calls: list[tuple[str, str, bytes]] = []

    def exchange(device_id: str, viewer_id: str, offer: bytes) -> WebRtcExchange:
        calls.append((device_id, viewer_id, offer))
        return WebRtcExchange(b"v=0\r\na=answer\r\n", "a" * 32)

    monkeypatch.setattr(context, "exchange_camera_webrtc", exchange)
    client = TestClient(create_app(context))

    response = client.post(
        f"/api/devices/{DEVICE_A}/camera/webrtc",
        content=b"v=0\r\na=offer\r\n",
        headers={"Content-Type": "application/sdp", "X-Relay-Viewer-ID": "a" * 32},
    )

    assert response.status_code == 201
    assert response.headers["content-type"].startswith("application/sdp")
    assert response.headers["x-relay-webrtc-session"] == "a" * 32
    assert response.content == b"v=0\r\na=answer\r\n"
    assert calls == [(DEVICE_A, "a" * 32, b"v=0\r\na=offer\r\n")]
    assert client.post(f"/api/devices/{DEVICE_A}/camera/webrtc", json={"src": "bad"}).status_code == 415
    assert client.post(
        "/api/devices/UNKNOWN/camera/webrtc",
        content=b"v=0",
        headers={"Content-Type": "application/sdp"},
    ).status_code == 404


def test_api_masks_credentials_and_sanitizes_raw_state(client: TestClient) -> None:
    """No learned password or full username can escape through read endpoints."""
    paths = (
        "/api/devices",
        f"/api/devices/{DEVICE_A}",
        f"/api/state?device_id={DEVICE_A}",
        "/api/status",
        "/api/logs",
        f"/api/queues?device_id={DEVICE_B}",
    )
    payload = " ".join(client.get(path).text for path in paths)

    for secret in (
        PASSWORD_A,
        PASSWORD_B,
        PASSWORD_C,
        QUEUE_SECRET,
        USERNAME_A,
        "must-not-appear",
        "internal-region",
        "private.example.invalid",
    ):
        assert secret not in payload
    assert "<redacted>" in client.get(f"/api/state?device_id={DEVICE_A}").text


def test_devices_endpoint_lists_every_device_with_its_own_status(client: TestClient) -> None:
    """The devices table shows all three, each with its own local/cloud state."""
    payload = client.get("/api/devices").json()
    rows = {row["device_id"]: row for row in payload["devices"]}

    assert set(rows) == {DEVICE_A, DEVICE_B, DEVICE_C}
    assert rows[DEVICE_A]["local_state"] == "LOCAL_ONLINE"
    assert rows[DEVICE_A]["cloud_state"] == "ONLINE"
    assert rows[DEVICE_B]["local_state"] == "LOCAL_ONLINE"
    assert rows[DEVICE_B]["cloud_state"] == "DISCONNECTED"
    assert rows[DEVICE_A]["rssi"] == -43
    assert rows[DEVICE_B]["rssi"] == -51


def test_summary_aggregates_across_devices(client: TestClient) -> None:
    """The header counts come from the individual device rows."""
    summary = client.get("/api/devices").json()["summary"]

    assert summary["known"] == 3
    assert summary["bridged"] == 3
    assert summary["local_online"] == 2
    assert summary["cloud_online"] == 1
    assert summary["cloud_degraded"] == 1, (
        "only B is degraded: C is absent, so having no cloud session is expected, not a fault"
    )
    assert summary["queue_pending"] == 1


def test_queue_depth_is_attributed_to_the_right_device(client: TestClient) -> None:
    """B's backlog must not be reported against A."""
    rows = {row["device_id"]: row for row in client.get("/api/devices").json()["devices"]}

    assert rows[DEVICE_B]["queue_pending"] == 1
    assert rows[DEVICE_A]["queue_pending"] == 0
    assert rows[DEVICE_C]["queue_pending"] == 0


def test_metrics_are_isolated_between_devices(client: TestClient) -> None:
    """A's NTP observation is not visible on B."""
    ntp_a = client.get(f"/api/ntp?device_id={DEVICE_A}").json()
    ntp_b = client.get(f"/api/ntp?device_id={DEVICE_B}").json()

    assert ntp_a["requests_observed"] == 1
    assert ntp_b["requests_observed"] == 0


def test_state_is_scoped_to_the_requested_device(client: TestClient) -> None:
    """A's raw messages must not appear under B."""
    state_a = client.get(f"/api/state?device_id={DEVICE_A}").json()
    state_b = client.get(f"/api/state?device_id={DEVICE_B}").json()

    assert state_a["counts"]["raw_messages"] == 2
    assert state_b["counts"]["raw_messages"] == 0


def test_device_detail_covers_one_device_only(client: TestClient) -> None:
    """The detail view bundles that device's cloud, queues, state and NTP."""
    detail = client.get(f"/api/devices/{DEVICE_B}").json()

    assert detail["device"]["device_id"] == DEVICE_B
    assert detail["cloud"]["metrics"]["device_id"] == DEVICE_B
    assert detail["queues"]["device_to_cloud"]["pending"] == 1
    assert detail["ntp"]["device_id"] == DEVICE_B
    assert all(event["device_id"] == DEVICE_B for event in detail["cloud"]["events"])


def test_device_detail_keeps_schedule_and_controls_isolated(client: TestClient) -> None:
    """A device view only projects its own desired controls and schedule."""
    detail_a = client.get(f"/api/devices/{DEVICE_A}").json()
    detail_b = client.get(f"/api/devices/{DEVICE_B}").json()

    desired_a = {item["key"]: item["value"] for item in detail_a["state"]["desired"]}
    assert desired_a["motionDetectionSwitch"] is False
    plan_a = detail_a["state"]["feeding_plans"]["plans"][0]
    assert plan_a["planId"] == DEVICE_A_PLANS[0]["planId"]
    assert plan_a["tutkP2pRegion"] == "<redacted>"
    assert plan_a["credentialHint"] == "<redacted>"
    assert plan_a["internalUrl"] == "<redacted>"
    assert detail_b["state"]["feeding_plans"]["plans"] == DEVICE_B_PLANS


def test_unknown_device_detail_is_404(client: TestClient) -> None:
    """An unknown id is refused rather than answered with another device's data."""
    assert client.get("/api/devices/NOT-A-DEVICE").status_code == 404


def test_device_html_routes_validate_device_ids(client: TestClient) -> None:
    """Fleet and known-device pages are served, unknown or unsafe ids are refused."""
    fleet = client.get("/devices")
    detail = client.get(f"/devices/{DEVICE_A}")

    assert fleet.status_code == 200
    assert detail.status_code == 200
    assert 'id="application"' in detail.text
    assert client.get("/devices/UNKNOWN").status_code == 404
    assert client.get("/devices/%2E%2E").status_code == 404


@pytest.mark.parametrize("path", ("/", "/settings", "/devices"))
def test_global_deep_links_return_the_dashboard_shell(client: TestClient, path: str) -> None:
    """Every global URL can be loaded or refreshed without relying on prior navigation."""
    response = client.get(path)

    assert response.status_code == 200
    assert 'id="application"' in response.text
    assert "function renderRoute()" in response.text


def test_devices_api_contract_supports_one_and_many_device_fleet_rendering(client: TestClient) -> None:
    """The fleet renderer receives the exact rows/summary shape it validates."""
    payload = client.get("/api/devices").json()

    assert isinstance(payload["devices"], list)
    assert isinstance(payload["summary"], dict)
    assert [row["device_id"] for row in payload["devices"]] == [DEVICE_A, DEVICE_B, DEVICE_C]
    assert all("queue_pending" in row and "cloud_state" in row for row in payload["devices"])
    assert "schedule" not in payload["devices"][0]


def test_daily_projections_hide_diagnostics_but_keep_feeder_actions(client: TestClient) -> None:
    """The normal UI API contains daily state without identity or raw payloads."""
    home = client.get("/api/home").json()
    daily = client.get(f"/api/devices/{DEVICE_A}/daily").json()

    assert isinstance(home["devices"][0]["schedule"], list)
    assert set(home["devices"][0]["camera"]) == {
        "available",
        "online",
        "webrtc",
        "bridge_registered",
        "go2rtc_reachable",
    }
    assert {"ip", "mac", "username", "client_id", "firmware"}.isdisjoint(daily["device"])
    assert "raw_messages" not in daily["state"]
    assert "controls" in daily and "schedule_plans" in daily["state"]
    assert {"mqttAddr", "httpsAddr", "tutkP2pRegion"}.isdisjoint(
        {entry["key"] for entry in daily["state"]["desired"]}
    )
    allowed_plan_keys = {
        "planId",
        "executionTime",
        "grainNum",
        "enableAudio",
        "audioTimes",
        "repeatDay",
        "syncTime",
    }
    assert all(
        set(entry["plan"]).issubset(allowed_plan_keys)
        for entry in daily["state"]["schedule_plans"]
    )
    assert "internal-region" not in json.dumps(daily)
    assert "must-not-appear" not in json.dumps(daily)
    assert "private.example.invalid" not in json.dumps(daily)


def test_empty_devices_api_contract_is_renderable(
    make_config: RelayConfigFactory,
) -> None:
    """An empty fleet retains the renderer's required list/summary shape."""
    config = make_config(web_enabled=True)
    registry = DeviceRegistry(config.device_registry_db_path)
    queue = MessageQueue(config.queue_db_path, config.max_queue_size)
    shadow = StateShadow(config.state_shadow_db_path)
    telemetry = RelayTelemetry()
    logs = RingBufferLogHandler()
    presence = DevicePresenceTracker()
    devices = DeviceManager(
        config, registry, queue, shadow, StateCache(config.state_cache_path), telemetry, presence
    )
    client = TestClient(
        create_app(DashboardContext(config, registry, queue, shadow, telemetry, logs, devices, presence))
    )
    try:
        payload = client.get("/api/devices").json()
        assert payload["devices"] == []
        assert payload["summary"]["known"] == 0
        assert "No PETLIBRO feeder has connected yet." in DASHBOARD_HTML
    finally:
        queue.close()
        registry.close()
        shadow.close()


def test_never_connected_device_is_shown_as_offline(client: TestClient) -> None:
    """A device known only from the registry must not read as online."""
    detail = client.get(f"/api/devices/{DEVICE_C}").json()

    assert detail["device"]["cloud_state"] == "DISCONNECTED"
    assert detail["cloud"]["metrics"]["upstream"]["availability"]["1h"] is None


def test_cloud_endpoint_reports_each_device_separately(client: TestClient) -> None:
    """The Cloud tab shows one upstream block per device that has a session."""
    payload = client.get("/api/cloud").json()
    states = {item["device_id"]: item["upstream"]["state"] for item in payload["devices"]}

    assert states == {DEVICE_A: "ONLINE", DEVICE_B: "DISCONNECTED"}


def test_pure_pipe_mode_is_explicit(client: TestClient) -> None:
    """The default (responder off) remains visible rather than implied."""
    status = client.get("/api/status").json()

    assert status["local_responder"]["enabled"] is False
    assert status["relay"]["mode"] == "PURE_PIPE"


def test_ntp_request_without_cloud_reply_is_shown_as_session_establishment(
    client: TestClient,
) -> None:
    """NTP is visible as a request; no reply is inferred when none was received."""
    payload = client.get(f"/api/ntp?device_id={DEVICE_A}").json()

    assert payload["trigger"] == "session_establishment"
    assert payload["requests_observed"] == 1
    assert payload["cloud_ntp_sync_responses"] == 0
    assert payload["last_request"]["cmd"] == "NTP"
    assert payload["last_ntp_sync"] is None


def test_dashboard_exposes_only_narrow_confirmed_control_write_routes(
    dashboard: tuple[DashboardContext, RingBufferLogHandler],
) -> None:
    """No generic MQTT or arbitrary-control write endpoint can reach HTTP."""
    context, _ = dashboard
    app = create_app(context)

    write_routes = {
        (route.path, tuple(sorted(route.methods or set())))
        for route in app.routes
        if route.methods is not None and not route.methods <= {"GET", "HEAD"}
    }

    assert write_routes == {
        ("/api/devices/{device_id}/controls/bowl", ("PATCH",)),
        ("/api/devices/{device_id}/controls/camera", ("PATCH",)),
        ("/api/devices/{device_id}/controls/feeding-video", ("PATCH",)),
        ("/api/devices/{device_id}/controls/light", ("PATCH",)),
        ("/api/devices/{device_id}/controls/motion", ("PATCH",)),
        ("/api/devices/{device_id}/controls/sound", ("PATCH",)),
        ("/api/devices/{device_id}/controls/sound-detection", ("PATCH",)),
        ("/api/devices/{device_id}/controls/video", ("PATCH",)),
        ("/api/devices/{device_id}/dispense", ("POST",)),
        ("/api/devices/{device_id}/camera/webrtc", ("POST",)),
        ("/api/devices/{device_id}/camera/webrtc/{session_id}", ("DELETE",)),
        ("/api/devices/{device_id}/camera/viewers/{viewer_id}", ("DELETE",)),
        ("/api/devices/{device_id}/camera/viewers/{viewer_id}", ("POST",)),
        ("/api/devices/{device_id}/camera/viewers/{viewer_id}", ("PUT",)),
        ("/api/devices/{device_id}/schedule", ("POST",)),
        ("/api/devices/{device_id}/schedule/{plan_id}", ("DELETE",)),
        ("/api/devices/{device_id}/schedule/{plan_id}", ("PATCH",)),
    }
    assert all("mqtt/publish" not in route.path for route in app.routes)
    assert all(route.path != "/api/devices/{device_id}/controls" for route in app.routes)


def test_daily_ui_uses_readable_cards_and_explicit_advanced_debug() -> None:
    """The refactor keeps daily tasks simple and leaves diagnostics explicit."""
    from petlibro_relay.web.user_interface import DASHBOARD_JAVASCRIPT

    assert "function renderHome()" in DASHBOARD_JAVASCRIPT
    assert "function renderAdvanced(detail)" in DASHBOARD_JAVASCRIPT
    assert "State and raw messages" in DASHBOARD_JAVASCRIPT
    assert "Device cloud events" in DASHBOARD_JAVASCRIPT
    assert "Device logs" in DASHBOARD_JAVASCRIPT
    assert "Raw JSON" not in DASHBOARD_JAVASCRIPT


def test_device_ui_preserves_camera_schedule_and_typed_settings() -> None:
    """Daily device tabs retain all established feeder functionality."""
    from petlibro_relay.web.user_interface import DASHBOARD_JAVASCRIPT

    for marker in (
        "function renderCamera(detail)",
        "function renderSchedule(detail)",
        "function renderSettings(detail)",
        "camera&&camera.bridge_registered&&camera.go2rtc_reachable",
        "startCamera(detail.device.device_id)",
        "data-add-plan",
        "data-edit-plan",
        "data-disable-plan",
        "data-delete-plan",
    ):
        assert marker in DASHBOARD_JAVASCRIPT
    assert "MANUAL_FEEDING_SERVICE" not in DASHBOARD_JAVASCRIPT


def test_camera_lifecycle_is_singleton_and_navigation_safe() -> None:
    """Only the validated WHEP lifecycle can create or release a viewer."""
    from petlibro_relay.web.user_interface import DASHBOARD_JAVASCRIPT

    assert DASHBOARD_JAVASCRIPT.count("async function startCamera(") == 1
    assert DASHBOARD_JAVASCRIPT.count("function closeCamera(") == 1
    assert "player.abort.abort()" in DASHBOARD_JAVASCRIPT
    assert "clearInterval(player.heartbeatTimer)" in DASHBOARD_JAVASCRIPT
    assert "window.addEventListener('pagehide',()=>{closeCamera();closeHomeCameras()})" in DASHBOARD_JAVASCRIPT
    assert "document.addEventListener('visibilitychange'" in DASHBOARD_JAVASCRIPT


def test_polling_preserves_device_scoped_schedule_and_setting_drafts() -> None:
    """Drafts belong to the selected device and block destructive rerenders."""
    from petlibro_relay.web.user_interface import DASHBOARD_JAVASCRIPT

    assert "draft&&draft.deviceId===detail.device.device_id&&root.querySelector('#schedule-editor')" in DASHBOARD_JAVASCRIPT
    assert "`${detail.device.device_id}:${group.path}`" in DASHBOARD_JAVASCRIPT
    assert "state.drafts.schedule=null;state.drafts.settings={}" in DASHBOARD_JAVASCRIPT
    assert "input.addEventListener('change'" in DASHBOARD_JAVASCRIPT


def test_home_uses_a_persistent_auto_starting_camera_player() -> None:
    """Home mounts the existing viewer lifecycle without a Camera route link."""
    from petlibro_relay.web.user_interface import DASHBOARD_JAVASCRIPT

    home_markup = DASHBOARD_JAVASCRIPT.split("function deviceCard", 1)[1].split(
        "function renderHome", 1
    )[0]
    assert "▶ Live" not in home_markup
    assert "data-home-camera" in home_markup
    assert "Open feeder settings" in home_markup
    assert "deviceSettingsUrl(device.device_id)" in home_markup
    assert "async function startHomeCamera(" in DASHBOARD_JAVASCRIPT
    assert "async function startCameraPlayer(" in DASHBOARD_JAVASCRIPT
    assert "updateHomeCard(device)" in DASHBOARD_JAVASCRIPT
    assert "homeCameraElement" in DASHBOARD_JAVASCRIPT
    update_source = DASHBOARD_JAVASCRIPT.split("function updateHomeCard", 1)[1].split(
        "function homeCameraElement", 1
    )[0]
    assert "data-home-camera-slot" not in update_source


def test_modal_shell_can_host_a_typed_action_form() -> None:
    """The dialog outer shell is not a form, so dispense can use its own form."""
    assert '<dialog id="app-modal"' in DASHBOARD_HTML
    assert '<div class="modal-shell">' in DASHBOARD_HTML
    assert 'id="modal-dismiss"' in DASHBOARD_HTML
    assert '<form method="dialog" class="modal-shell">' not in DASHBOARD_HTML


def test_sse_emits_new_sanitized_log(
    dashboard: tuple[DashboardContext, RingBufferLogHandler],
) -> None:
    """The SSE generator publishes a newly appended safe log record."""
    context, logs = dashboard
    record = logging.makeLogRecord(
        {"msg": f"password={PASSWORD_A}", "levelno": logging.INFO, "levelname": "INFO"}
    )
    logs.emit(record)

    event = next(_stream_logs(context, after=0))

    assert event.startswith("id: 1\nevent: log\ndata: ")
    assert PASSWORD_A not in event
    assert "<redacted>" in event
