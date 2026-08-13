"""Mock-only coverage for grouped local settings and feeder schedule transactions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from test_sound_switch_control import DEVICE_A, ControlHarness, control_environment

from petlibro_relay.sound_switch_control import (
    _next_local_plan_id,
    _populate_duration_fields,
    _populate_time_fields,
)
from petlibro_relay.state_shadow import StateShadow


def test_grouped_light_builder_uses_dst_and_direct_duration(
    control_environment: tuple[object, ControlHarness, object, object, object],
) -> None:
    """Local HH:MM fields gain real timezone UTC fields and an unclamped duration."""
    _, harness, _, _, _ = control_environment
    values = {"lightingStartTime": "23:59", "lightingEndTime": "23:58"}
    from zoneinfo import ZoneInfo

    _populate_time_fields(values, ZoneInfo("Europe/Paris"))
    _populate_duration_fields(values)

    assert values["lightingTimes"] == -1
    assert values["lightingStartTimeUtc"] != "23:59"
    assert harness.published == []


def test_schedule_create_allocates_negative_ids_and_persists(
    control_environment: tuple[TestClient, ControlHarness, object, object, object],
) -> None:
    """Each device gets -1, -2, … and confirmed snapshots survive storage reopen."""
    client, harness, shadow, _, _ = control_environment
    payload = {
        "executionTime": "22:21",
        "grainNum": 12,
        "enableAudio": False,
        "audioTimes": 2,
        "repeatDay": [7, 1, 2, 3, 4, 5, 6],
    }

    first = client.post(f"/api/devices/{DEVICE_A}/schedule", json=payload)
    assert first.status_code == 200
    first_packet = json.loads(harness.published[-1][2])
    assert first_packet["cmd"] == "FEEDING_PLAN_SERVICE"
    assert first_packet["plans"][0]["planId"] == -1
    assert first_packet["plans"][0]["repeatDay"] == [1, 2, 3, 4, 5, 6, 7]
    assert first_packet["plans"][0]["syncTime"] % 1000 == 0

    second_payload = {**payload, "executionTime": "22:22"}
    second = client.post(f"/api/devices/{DEVICE_A}/schedule", json=second_payload)
    assert second.status_code == 200
    second_packet = json.loads(harness.published[-1][2])
    assert {plan["planId"] for plan in second_packet["plans"]} == {-1, -2}
    assert len(shadow.get_schedule_plans(DEVICE_A)) == 2


def test_schedule_edit_delete_never_and_collision_validation(
    control_environment: tuple[TestClient, ControlHarness, object, object, object],
) -> None:
    """Plans keep their ID, Never is empty days, and active duplicate times fail."""
    client, harness, _, _, _ = control_environment
    first = {
        "executionTime": "08:00", "grainNum": 1, "enableAudio": True,
        "audioTimes": 1, "repeatDay": [1],
    }
    assert client.post(f"/api/devices/{DEVICE_A}/schedule", json=first).status_code == 200
    collision = client.post(f"/api/devices/{DEVICE_A}/schedule", json={**first, "repeatDay": [2]})
    assert collision.status_code == 409
    never = client.patch(f"/api/devices/{DEVICE_A}/schedule/-1", json={"repeatDay": []})
    assert never.status_code == 200
    packet = json.loads(harness.published[-1][2])
    assert packet["plans"][0]["planId"] == -1
    assert packet["plans"][0]["repeatDay"] == []
    assert client.delete(f"/api/devices/{DEVICE_A}/schedule/-1").status_code == 200
    assert json.loads(harness.published[-1][2])["plans"] == []


def test_schedule_local_ids_are_per_device() -> None:
    """Negative IDs intentionally repeat across device namespaces."""
    assert _next_local_plan_id([]) == -1
    assert _next_local_plan_id([{"planId": -1}]) == -2


def test_schedule_storage_is_persistent_and_namespaced_by_device(tmp_path: Path) -> None:
    """Two devices can both own -1 without leaking a local snapshot."""
    db_path = tmp_path / "state.sqlite3"
    shadow = StateShadow(str(db_path))
    plan_a = {
        "planId": -1,
        "executionTime": "08:00",
        "grainNum": 1,
        "enableAudio": False,
        "audioTimes": 1,
        "repeatDay": [1],
        "syncTime": 1_786_659_757_000,
    }
    plan_b = {**plan_a, "executionTime": "18:00"}
    shadow.replace_local_schedule_plans(DEVICE_A, [plan_a])
    shadow.replace_local_schedule_plans("OTHERDEVICE000000001", [plan_b])
    shadow.close()

    reopened = StateShadow(str(db_path))
    assert reopened.get_schedule_plans(DEVICE_A)[0].plan == plan_a
    assert reopened.get_schedule_plans("OTHERDEVICE000000001")[0].plan == plan_b
    reopened.close()


def test_shadow_merges_partial_cloud_deltas_without_erasing_local_confirmation(
    control_environment: tuple[TestClient, ControlHarness, object, object, object],
) -> None:
    """One-field cloud deltas do not remove unrelated desired or ACKed fields."""
    _, _, shadow, _, _ = control_environment
    shadow.update_desired(DEVICE_A, {"filterLedSwitch": True, "soundSwitch": False})
    shadow.update_local_confirmed(DEVICE_A, {"soundSwitch": True})
    shadow.update_desired(DEVICE_A, {"filterLedSwitch": False})

    assert shadow.get_desired(DEVICE_A)["soundSwitch"] is False
    assert shadow.get_desired(DEVICE_A)["filterLedSwitch"] is False
    assert shadow.get_local_confirmed(DEVICE_A)["soundSwitch"] is True


@pytest.mark.parametrize(
    ("path", "payload", "expected"),
    [
        ("motion", {"motionDetectionSensitivity": "HIGH", "motionDetectionRange": "LARGE"}, {"motionDetectionSensitivity": "HIGH", "motionDetectionRange": "LARGE"}),
        ("sound-detection", {"soundDetectionSwitch": True, "soundDetectionSensitivity": "LOW"}, {"soundDetectionSwitch": True, "soundDetectionSensitivity": "LOW"}),
        ("sound", {"volume": 100, "soundStartTime": "19:50", "soundEndTime": "23:53"}, {"volume": 100, "soundTimes": 243}),
        ("light", {"lightSwitch": True, "lightingStartTime": "23:59", "lightingEndTime": "23:58"}, {"lightSwitch": True, "lightingTimes": -1}),
        ("camera", {"resolution": "P1080", "nightVision": "CLOSE"}, {"resolution": "P1080", "nightVision": "CLOSE"}),
        ("video", {"videoRecordMode": "MOTION_DETECTION", "videoWatermarkSwitch": True}, {"videoRecordMode": "MOTION_DETECTION", "videoWatermarkSwitch": True}),
        ("feeding-video", {"automaticRecording": 5, "afterManualFeedingTime": 1}, {"automaticRecording": 5, "afterManualFeedingTime": 1}),
        ("bowl", {"bowlMode": "DOUBLE_BOWL"}, {"bowlMode": "DOUBLE_BOWL"}),
    ],
)
def test_typed_group_routes_build_only_allowlisted_attr_set_payloads(
    control_environment: tuple[TestClient, ControlHarness, object, object, object],
    path: str,
    payload: dict[str, object],
    expected: dict[str, object],
) -> None:
    """Every settings group uses ATTR_SET_SERVICE and preserves typed values."""
    client, harness, _, _, _ = control_environment

    response = client.patch(f"/api/devices/{DEVICE_A}/controls/{path}", json=payload)

    assert response.status_code == 200
    packet = json.loads(harness.published[-1][2])
    assert packet["cmd"] == "ATTR_SET_SERVICE"
    assert isinstance(packet["msgId"], str)
    assert isinstance(packet["ts"], int)
    for key, value in expected.items():
        assert packet[key] == value
    assert "cloudVideoRecordSwitch" not in packet


def test_setting_routes_validate_strict_enums_and_ranges(
    control_environment: tuple[TestClient, ControlHarness, object, object, object],
) -> None:
    """No route accepts arbitrary fields, enum values, or unsafe numeric ranges."""
    client, _, _, _, _ = control_environment
    assert client.patch(f"/api/devices/{DEVICE_A}/controls/camera", json={"resolution": "P4K"}).status_code == 422
    assert client.patch(f"/api/devices/{DEVICE_A}/controls/sound", json={"volume": 101}).status_code == 422
    assert client.patch(f"/api/devices/{DEVICE_A}/controls/feeding-video", json={"automaticRecording": 0}).status_code == 422
    assert client.patch(f"/api/devices/{DEVICE_A}/controls/bowl", json={"bowlMode": "TRIPLE"}).status_code == 422
