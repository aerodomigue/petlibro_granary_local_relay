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
from petlibro_relay.camera import CameraStatus
from petlibro_relay.device_manager import DeviceManager
from petlibro_relay.device_presence import DevicePresenceTracker
from petlibro_relay.device_registry import DeviceIdentity, DeviceRegistry
from petlibro_relay.message_queue import MessageQueue
from petlibro_relay.observability.log_buffer import RingBufferLogHandler
from petlibro_relay.observability.telemetry import RelayTelemetry
from petlibro_relay.state_cache import StateCache
from petlibro_relay.state_shadow import StateShadow
from petlibro_relay.web.app import _stream_logs, create_app
from petlibro_relay.web.context import DashboardContext
from petlibro_relay.web.static import DASHBOARD_HTML

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
    calls: list[tuple[str, bytes]] = []

    def exchange(device_id: str, offer: bytes) -> bytes:
        calls.append((device_id, offer))
        return b"v=0\r\na=answer\r\n"

    monkeypatch.setattr(context, "exchange_camera_webrtc", exchange)
    client = TestClient(create_app(context))

    response = client.post(
        f"/api/devices/{DEVICE_A}/camera/webrtc",
        content=b"v=0\r\na=offer\r\n",
        headers={"Content-Type": "application/sdp"},
    )

    assert response.status_code == 201
    assert response.headers["content-type"].startswith("application/sdp")
    assert response.content == b"v=0\r\na=answer\r\n"
    assert calls == [(DEVICE_A, b"v=0\r\na=offer\r\n")]
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

    for secret in (PASSWORD_A, PASSWORD_B, PASSWORD_C, QUEUE_SECRET, USERNAME_A):
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
    assert detail_a["state"]["feeding_plans"]["plans"] == DEVICE_A_PLANS
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
    assert "device-page" in detail.text
    assert client.get("/devices/UNKNOWN").status_code == 404
    assert client.get("/devices/%2E%2E").status_code == 404


@pytest.mark.parametrize("path", ("/", "/cloud", "/devices", "/queues", "/state", "/ntp", "/logs", "/system"))
def test_global_deep_links_return_the_dashboard_shell(client: TestClient, path: str) -> None:
    """Every global URL can be loaded or refreshed without relying on prior navigation."""
    response = client.get(path)

    assert response.status_code == 200
    assert 'id="global-pages"' in response.text
    assert "applyRoute()" in response.text


def test_devices_api_contract_supports_one_and_many_device_fleet_rendering(client: TestClient) -> None:
    """The fleet renderer receives the exact rows/summary shape it validates."""
    payload = client.get("/api/devices").json()

    assert isinstance(payload["devices"], list)
    assert isinstance(payload["summary"], dict)
    assert [row["device_id"] for row in payload["devices"]] == [DEVICE_A, DEVICE_B, DEVICE_C]
    assert all("queue_pending" in row and "cloud_state" in row for row in payload["devices"])


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
        assert "No devices known yet. Waiting for a PETLIBRO device" in DASHBOARD_HTML
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
        ("/api/devices/{device_id}/camera/webrtc", ("POST",)),
        ("/api/devices/{device_id}/schedule", ("POST",)),
        ("/api/devices/{device_id}/schedule/{plan_id}", ("DELETE",)),
        ("/api/devices/{device_id}/schedule/{plan_id}", ("PATCH",)),
    }
    assert all("mqtt/publish" not in route.path for route in app.routes)
    assert all(route.path != "/api/devices/{device_id}/controls" for route in app.routes)


def test_ui_keeps_raw_data_behind_explicit_debug_controls() -> None:
    """The page provides formatted observability views and explicit raw-data controls."""
    for helper in (
        "formatDuration",
        "formatAge",
        "formatTimestamp",
        "formatPercent",
        "formatBoolean",
        "statusBadge",
        "escapeHtml",
    ):
        assert helper in DASHBOARD_HTML
    assert "Raw JSON" in DASHBOARD_HTML
    assert "EventSource('/api/logs/stream')" in DASHBOARD_HTML


def test_ui_renders_a_fleet_table_and_dedicated_device_pages() -> None:
    """Fleet listing links to device-scoped tabs instead of embedding a detail pane."""
    assert "device-row" in DASHBOARD_HTML
    assert 'href="/devices"' in DASHBOARD_HTML
    assert "const DEVICE_TABS=" in DASHBOARD_HTML
    assert "renderDevicePage" in DASHBOARD_HTML
    assert "/api/devices/${encodeURIComponent(deviceId)}" in DASHBOARD_HTML
    assert "devicePicker" in DASHBOARD_HTML
    assert "requestMotionDetectionSwitch" in DASHBOARD_HTML
    assert "motion-detection-switch-toggle" in DASHBOARD_HTML


def test_ui_initial_route_activates_the_matching_section_and_supports_history() -> None:
    """Direct links do not leave the target section hidden behind the shell."""
    for marker in (
        "const GLOBAL_ROUTES=",
        "function tabForPath",
        "function applyRoute",
        "byId(tab).classList.toggle('active',!deviceId&&tab===runtime.active)",
        "history.pushState",
        "window.addEventListener('popstate',applyRoute)",
        "bindRoutes()",
        "Failed to render",
    ):
        assert marker in DASHBOARD_HTML


def test_device_ui_has_typed_controls_schedule_camera_and_reused_views() -> None:
    """Device sections expose only the typed ACK-confirmed controls and plans."""
    for marker in (
        "controlsMarkup",
        "scheduleMarkup",
        "cameraMarkup",
        "stateMarkup",
        "logPanel('device-log',deviceId)",
        "go2rtc stream status",
        "go2rtc_reachable",
        "WebRTC via local go2rtc",
        "camera/webrtc",
        "renderLogLines('device-log',deviceId)",
        "schedule-edit",
        "schedulePayload",
        "Local MQTT schedule",
        "Local confirmation differs from cloud desired.",
    ):
        assert marker in DASHBOARD_HTML
    assert HTML_INJECTION_VALUE not in DASHBOARD_HTML
    assert "escapeHtml" in DASHBOARD_HTML


def test_camera_player_survives_device_status_refreshes() -> None:
    """Camera polling preserves the active WebRTC video element and peer connection."""
    assert "player&&player.deviceId===deviceId&&byId('camera-player')" in DASHBOARD_HTML
    assert "runtime.deviceDetail=detail;updateCameraPlayerStatus(detail.camera);return" in DASHBOARD_HTML
    assert "video.onloadeddata=()=>{if(runtime.cameraPlayer===player)setCameraPlayerState('Live','live')}" in DASHBOARD_HTML


def test_controls_and_schedule_use_conditional_human_friendly_components() -> None:
    """The device UI hides protocol details behind explicit control components."""
    for marker in (
        "setting-grid",
        "setting-card",
        "control-form",
        "data-show-when",
        "Enable motion detection",
        "Enable sound detection",
        "Enable device sound",
        "720p",
        "Always on",
        "Cloud recording",
        "schedule-editor",
        "Create feeding plan",
        "Custom days",
        "Local MQTT schedules",
        "Delete this feeding plan? This cannot be undone locally.",
        "scheduleDraftDays",
    ):
        assert marker in DASHBOARD_HTML
    assert "Days (1=Mon … 7=Sun)" not in DASHBOARD_HTML


def test_controls_render_and_condition_initialization_are_idempotent() -> None:
    """Controls markup renders and does not self-trigger observer mutations."""
    script = DASHBOARD_HTML.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    controls_start = script.rindex("function effectiveValues")
    controls_end = script.rindex("function readControlValue")
    conditions_start = controls_end
    conditions_end = script.rindex("function controlPayload")
    fixture = {
        "state": {
            "desired": [
                {"key": "soundSwitch", "value": True},
                {"key": "soundAgingType", "value": 1},
                {"key": "volume", "value": 37},
                {"key": "motionDetectionSwitch", "value": False},
            ],
            "reported": [],
            "local_confirmed": [],
        },
        "controls": {
            "soundSwitch": {
                "writable": True,
                "device_online": True,
                "pending": False,
            },
        },
    }
    node_test = f"""
const runtime = {{ controlSaving: {{}}, controlFeedback: {{}} }};
const escapeHtml = value => String(value ?? '—');
const desiredValues = state => Object.fromEntries((state.desired || []).map(item => [item.key, item.value]));
const confirmedValues = state => Object.fromEntries((state.local_confirmed || []).map(item => [item.key, item.value]));
const reportedValues = state => Object.fromEntries((state.reported || []).map(item => [item.key, item.value]));
const card = (title, content) => `<article><h2>${{title}}</h2>${{content}}</article>`;
{script[controls_start:controls_end]}
globalThis.CSS = {{ escape: value => value }};
globalThis.HTMLInputElement = class {{}};
globalThis.HTMLSelectElement = class {{}};
{script[conditions_start:conditions_end]}
const fixture = {json.dumps(fixture)};
const markup = controlsMarkup(fixture.state, fixture.controls);
if (!markup.includes('Enable device sound') || !markup.includes('value="37"')) {{
  throw new Error('Controls markup did not render the realistic device fixture.');
}}
class FakeInput extends HTMLInputElement {{
  constructor(name, type, value, checked) {{
    super();
    this.name = name;
    this.type = type;
    this.value = value;
    this.checked = checked;
  }}
}}
const soundSwitch = new FakeInput('soundSwitch', 'checkbox', '', true);
const volume = new FakeInput('volume', 'range', '37', false);
const visibility = {{
  dataset: {{ showWhen: 'soundSwitch:true' }},
  hidden: null,
  classList: {{ toggle: (_name, hidden) => {{ visibility.hidden = hidden; }} }},
}};
const output = {{
  writes: 0,
  value: '0%',
  get textContent() {{ return this.value; }},
  set textContent(value) {{ this.writes += 1; this.value = value; }},
}};
const form = {{
  querySelectorAll(selector) {{
    if (selector === '[data-show-when]') return [visibility];
    if (selector === 'input[type=range]') return [volume];
    if (selector === '[name="soundSwitch"]') return [soundSwitch];
    return [];
  }},
  querySelector(selector) {{
    return selector === '[data-range-output="volume"]' ? output : null;
  }},
}};
updateControlConditions(form);
updateControlConditions(form);
if (visibility.hidden !== false) throw new Error('Initial visibility state was not applied.');
if (output.writes !== 1 || output.textContent !== '37%') {{
  throw new Error(`Condition initialization was not idempotent: ${{output.writes}} writes.`);
}}
"""
    result = subprocess.run(
        ["node", "-e", node_test],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_schedule_drafts_survive_polling_for_create_and_edit() -> None:
    """Polling server state never replaces an active schedule editor's local draft."""
    script = DASHBOARD_HTML.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    schedule_start = script.rindex("function scheduleRepeatMode")
    schedule_end = script.rindex("async function requestScheduleEditor")
    node_test = f"""
const runtime = {{ scheduleEditor: null, scheduleFeedback: null }};
const escapeHtml = value => String(value ?? '—');
const readControlValue = (form, key) => form.values[key];
const timeField = (values, key) => `<input name="${{key}}" value="${{values[key] ?? ''}}">`;
const numberField = (values, key) => `<input name="${{key}}" value="${{values[key] ?? ''}}">`;
const checkboxRow = (values, key) => `<input name="${{key}}" type="checkbox" ${{values[key] === true ? 'checked' : ''}}>`;
const conditional = (_condition, content) => content;
const radioChoice = (values, key, _label, options) => options.map(([value]) => `<input name="${{key}}" value="${{value}}" ${{values[key] === value ? 'checked' : ''}}>`).join('');
const scheduleDays = days => Array.isArray(days) ? days.join(',') : '—';
{script[schedule_start:schedule_end]}
function form(values, selectedDays) {{
  return {{
    values,
    querySelectorAll: selector => selector === 'input[name="repeatDay"]:checked'
      ? selectedDays.map(value => ({{ value: String(value) }})) : [],
  }};
}}
runtime.scheduleEditor = {{ mode: 'create', planId: null, draft: scheduleDraftFromPlan() }};
if (!scheduleMarkup({{ schedule_plans: [] }}).includes('value="every" checked')) {{
  throw new Error('Create editor no longer defaults to every day.');
}}
updateScheduleDraft(form({{
  executionTime: '12:34', grainNum: '4', enableAudio: true,
  audioTimes: '3', repeatMode: 'custom',
}}, [1, 3, 5]));
const createAfterPoll = scheduleMarkup({{ schedule_plans: [] }});
if (!createAfterPoll.includes('value="12:34"') || !createAfterPoll.includes('value="4"')) {{
  throw new Error('Create draft was replaced by the polling render.');
}}
if (!createAfterPoll.includes('value="custom" checked') || !createAfterPoll.includes('value="1" checked')) {{
  throw new Error('Create repeat draft was replaced by the polling render.');
}}
runtime.scheduleEditor = {{
  mode: 'edit', planId: 42,
  draft: scheduleDraftFromPlan({{ executionTime: '07:30', grainNum: 1, enableAudio: false, audioTimes: 1, repeatDay: [1, 2, 3, 4, 5, 6, 7] }}),
}};
updateScheduleDraft(form({{
  executionTime: '19:45', grainNum: '7', enableAudio: true,
  audioTimes: '2', repeatMode: 'never',
}}, []));
const editAfterPoll = scheduleMarkup({{
  schedule_plans: [{{ plan: {{ planId: 42, executionTime: '07:30', grainNum: 1, repeatDay: [1, 2, 3, 4, 5, 6, 7] }}, source: 'local' }}],
}});
if (!editAfterPoll.includes('value="19:45"') || !editAfterPoll.includes('value="7"')) {{
  throw new Error('Edit draft was replaced by the polling render.');
}}
if (!editAfterPoll.includes('value="never" checked')) {{
  throw new Error('Edit repeat draft was replaced by the polling render.');
}}
"""
    result = subprocess.run(
        ["node", "-e", node_test],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_control_draft_survives_server_render() -> None:
    """An unsaved Controls field is restored after a polling-driven rerender."""
    script = DASHBOARD_HTML.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    draft_start = script.rindex("function controlDraftKey")
    draft_end = script.rindex("function controlPayload")
    node_test = f"""
const runtime = {{ controlDrafts: {{}} }};
const currentDeviceId = () => 'DEVICE-A';
const CSS = {{ escape: value => value }};
const updateControlConditions = () => undefined;
globalThis.HTMLInputElement = class {{}};
globalThis.HTMLSelectElement = class {{}};
{script[draft_start:draft_end]}
class Input extends HTMLInputElement {{
  constructor(name, type, checked) {{
    super();
    this.name = name;
    this.type = type;
    this.checked = checked;
    this.value = checked ? 'true' : 'false';
    this.dataset = {{}};
  }}
}}
const edited = new Input('soundSwitch', 'checkbox', true);
const editingForm = {{ dataset: {{ controlPath: 'sound' }} }};
updateControlDraft(editingForm, edited);
const rerendered = new Input('soundSwitch', 'checkbox', false);
const renderedForm = {{
  dataset: {{ controlPath: 'sound' }},
  querySelectorAll: selector => selector === '[name="soundSwitch"]' ? [rerendered] : [],
}};
restoreControlDrafts({{ querySelectorAll: selector => selector === '.control-form' ? [renderedForm] : [] }}, 'DEVICE-A');
if (rerendered.checked !== true || rerendered.dataset.dirty !== 'true') {{
  throw new Error('Controls draft was replaced by the polling render.');
}}
"""
    result = subprocess.run(
        ["node", "-e", node_test],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


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
