"""Regression coverage for the user-facing dashboard composition."""

from __future__ import annotations

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


def test_polling_preserves_interactive_sections_and_camera() -> None:
    """A poll leaves active forms and an established camera element intact."""
    assert "state.drafts.schedule&&root.querySelector('#schedule-editor'))return" in DASHBOARD_JAVASCRIPT
    assert "state.drafts.deviceName!==undefined" in DASHBOARD_JAVASCRIPT
    assert "player&&player.deviceId===detail.device.device_id&&byId('camera-player')" in DASHBOARD_JAVASCRIPT
    assert "if(state.refresh.inFlight&&!force)return" in DASHBOARD_JAVASCRIPT


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
