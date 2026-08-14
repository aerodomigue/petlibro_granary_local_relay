"""Regression coverage for the user-facing dashboard composition."""

from __future__ import annotations

import subprocess

from petlibro_relay.web.static import DASHBOARD_HTML
from petlibro_relay.web.user_interface import DASHBOARD_JAVASCRIPT


def test_daily_navigation_hides_diagnostics_until_advanced_mode() -> None:
    """Normal device navigation contains only daily feeder tasks."""
    assert "['overview','Overview']" in DASHBOARD_JAVASCRIPT
    assert "['camera','Camera']" in DASHBOARD_JAVASCRIPT
    assert "['schedule','Schedule']" in DASHBOARD_JAVASCRIPT
    assert "['activity','Activity']" in DASHBOARD_JAVASCRIPT
    assert "['settings','Settings']" in DASHBOARD_JAVASCRIPT
    assert "...(state.advanced?[['advanced','Advanced']]:[])" in DASHBOARD_JAVASCRIPT
    assert "window.location.hash==='#advanced'" in DASHBOARD_JAVASCRIPT


def test_polling_preserves_interactive_sections_and_camera() -> None:
    """A poll leaves active forms and an established camera element intact."""
    assert "draft&&draft.deviceId===detail.device.device_id&&root.querySelector('#schedule-editor')" in DASHBOARD_JAVASCRIPT
    assert "state.drafts.deviceName!==undefined" in DASHBOARD_JAVASCRIPT
    assert "player&&player.deviceId===detail.device.device_id&&byId('camera-player')" in DASHBOARD_JAVASCRIPT
    assert "if(state.refresh.inFlight&&!force)return" in DASHBOARD_JAVASCRIPT


def test_camera_autostart_uses_the_validated_bridge_readiness_gate() -> None:
    """A stream is created only after Camera mounts, not before it reports WebRTC."""
    assert "camera&&camera.bridge_registered&&camera.go2rtc_reachable" in DASHBOARD_JAVASCRIPT
    assert "camera&&camera.webrtc&&camera.bridge_registered" not in DASHBOARD_JAVASCRIPT
    assert "startCamera(detail.device.device_id)" in DASHBOARD_JAVASCRIPT


def test_daily_settings_restore_every_existing_typed_group() -> None:
    """The simplified navigation does not remove established feeder controls."""
    for path in (
        "motion",
        "sound-detection",
        "sound",
        "light",
        "camera",
        "video",
        "feeding-video",
        "bowl",
    ):
        assert f"path:'{path}'" in DASHBOARD_JAVASCRIPT
    assert "data-disable-plan" in DASHBOARD_JAVASCRIPT
    assert "toggleSchedule" in DASHBOARD_JAVASCRIPT


def test_manual_dispense_is_a_narrow_ack_backed_action() -> None:
    """The UI cannot choose a topic or arbitrary command for manual feeding."""
    assert "/dispense" in DASHBOARD_JAVASCRIPT
    assert "grainNum" in DASHBOARD_JAVASCRIPT
    assert "MANUAL_FEEDING_SERVICE" not in DASHBOARD_JAVASCRIPT


def test_dashboard_never_exposes_a_generic_mqtt_write_path() -> None:
    """The browser calls only typed device endpoints already owned by the API."""
    assert "mqtt/publish" not in DASHBOARD_HTML
    assert "topic:" not in DASHBOARD_JAVASCRIPT
    assert "camera/webrtc" in DASHBOARD_JAVASCRIPT
    assert "/controls/${encodeURIComponent(path)}" in DASHBOARD_JAVASCRIPT


def test_camera_uses_one_lifecycle_with_browser_fallback() -> None:
    """The refactor keeps one viewer lifecycle and supports HTTP dashboards."""
    assert DASHBOARD_JAVASCRIPT.count("async function startCamera(") == 1
    assert DASHBOARD_JAVASCRIPT.count("function closeCamera(") == 1
    assert "globalThis.crypto.randomUUID" in DASHBOARD_JAVASCRIPT
    assert "camera/viewers" in DASHBOARD_JAVASCRIPT
    assert "CAMERA_HIDDEN_CLOSE_DELAY_MS=15000" in DASHBOARD_JAVASCRIPT


def test_global_navigation_releases_a_camera_viewer() -> None:
    """Leaving Camera through Home or Settings cannot leave a WHEP viewer alive."""
    route_source = "function setRoute" + DASHBOARD_JAVASCRIPT.split(
        "function setRoute", 1
    )[1].split("function setDeviceTab", 1)[0]
    node_test = f"""
const state = {{ camera: {{ deviceId: 'DEVICE-A' }} }};
let closed = 0;
let homeClosed = 0;
const closeCamera = () => {{ closed += 1; state.camera = null; }};
const closeHomeCameras = () => {{ homeClosed += 1; }};
const history = {{ pushState: () => undefined }};
const refresh = () => undefined;
{route_source}
setRoute('/settings');
if (closed !== 1) throw new Error('Camera was not released when leaving the device route.');
if (homeClosed !== 1) throw new Error('Home viewers were not released when leaving Home.');
state.camera = {{ deviceId: 'DEVICE-A' }};
setRoute('/devices/DEVICE-A#camera');
if (closed !== 1) throw new Error('Camera was unnecessarily restarted for the same device.');
"""

    result = subprocess.run(["node", "-e", node_test], check=False, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_settings_wait_for_required_device_state_and_schedule_never_guesses_days() -> None:
    """The UI does not submit incomplete settings or invent a daily recurrence."""
    assert "!cap.required_state_available" in DASHBOARD_JAVASCRIPT
    assert "state.disabledScheduleDays" in DASHBOARD_JAVASCRIPT
    assert "Choose at least one repeat day before enabling this meal." in DASHBOARD_JAVASCRIPT
    assert "Choose at least one day, or select Never." in DASHBOARD_JAVASCRIPT
