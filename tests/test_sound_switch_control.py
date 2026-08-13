"""Safe mock-only coverage for the one confirmed interactive feeder control."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import RelayConfigFactory

from petlibro_relay.device_context import LOCAL_TO_UPSTREAM
from petlibro_relay.device_manager import DeviceManager
from petlibro_relay.device_presence import DevicePresenceTracker
from petlibro_relay.device_registry import DeviceIdentity, DeviceRegistry
from petlibro_relay.message_queue import MessageQueue
from petlibro_relay.mqtt_bridge import MqttBridge
from petlibro_relay.observability.log_buffer import RingBufferLogHandler
from petlibro_relay.observability.telemetry import RelayTelemetry
from petlibro_relay.state_cache import StateCache
from petlibro_relay.state_shadow import StateShadow
from petlibro_relay.sound_switch_control import ControlAckTimeoutError, SoundSwitchController
from petlibro_relay.web.app import create_app
from petlibro_relay.web.context import DashboardContext

DEVICE_A = "TESTDEVICE0000000001"
DEVICE_B = "TESTDEVICE0000000002"
PRODUCT_ID = "PLAF203"
SERVICE_POST_A = f"dl/{PRODUCT_ID}/{DEVICE_A}/device/service/post"


class ControlHarness:
    """In-process fake local broker that can ACK the exact published payload."""

    def __init__(self, controller: SoundSwitchController | None = None, ack_code: object | None = 0) -> None:
        self.controller = controller
        self.ack_code = ack_code
        self.ack_device_id: str | None = None
        self.ack_message_id: str | None = None
        self.published: list[tuple[str, str, bytes]] = []
        self.published_event = threading.Event()

    def publish(self, device_id: str, product_id: str, payload: bytes) -> bool:
        """Capture the local publication and optionally feed a fake device ACK back."""
        self.published.append((device_id, product_id, payload))
        self.published_event.set()
        if self.controller is not None and self.ack_code is not None:
            body = json.loads(payload)
            ack = json.dumps(
                {
                    "cmd": "ATTR_SET_SERVICE",
                    "msgId": self.ack_message_id or body["msgId"],
                    "code": self.ack_code,
                }
            ).encode()
            self.controller.observe_device_message(
                self.ack_device_id or device_id,
                f"dl/{product_id}/{self.ack_device_id or device_id}/device/service/post",
                ack,
            )
        return True


@pytest.fixture
def control_environment(
    make_config: RelayConfigFactory, tmp_path: Path
) -> tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager]:
    """Create isolated devices, state, and a fake local publisher for API tests."""
    config = make_config(web_enabled=True)
    registry = DeviceRegistry(config.device_registry_db_path)
    identity_a = DeviceIdentity(DEVICE_A, "USER-A", "password-a")
    identity_b = DeviceIdentity(DEVICE_B, "USER-B", "password-b")
    registry.record(identity_a)
    registry.record(identity_b)
    queue = MessageQueue(config.queue_db_path, config.max_queue_size)
    shadow = StateShadow(config.state_shadow_db_path)
    shadow.update_desired(DEVICE_A, {"soundSwitch": False, "soundAgingType": 1})
    shadow.update_desired(DEVICE_B, {"soundSwitch": False, "soundAgingType": 1})
    telemetry = RelayTelemetry()
    presence = DevicePresenceTracker()
    presence.session_opened(DEVICE_A)
    devices = DeviceManager(
        config, registry, queue, shadow, StateCache(config.state_cache_path), telemetry, presence
    )
    devices.ensure_device(identity_a)
    devices.ensure_device(identity_b)
    harness = ControlHarness()
    controller = SoundSwitchController(devices, presence, shadow, harness.publish, ack_timeout_seconds=0.15)
    harness.controller = controller
    context = DashboardContext(
        config,
        registry,
        queue,
        shadow,
        telemetry,
        RingBufferLogHandler(),
        devices,
        presence,
        controller,
    )
    client = TestClient(create_app(context))
    yield client, harness, shadow, queue, devices
    queue.close()
    registry.close()
    shadow.close()


def test_sound_control_builds_confirmed_payload_and_waits_for_ack(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
) -> None:
    """HTTP success means a same-device, same-msgId feeder ACK was observed."""
    client, harness, shadow, _, _ = control_environment

    response = client.patch(f"/api/devices/{DEVICE_A}/controls/sound", json={"enabled": True})

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "device_id": DEVICE_A,
        "control": "soundSwitch",
        "value": True,
        "device_ack": True,
        "cloud_sync_behavior": "confirmed",
    }
    assert len(harness.published) == 1
    device_id, product_id, raw_payload = harness.published[0]
    payload = json.loads(raw_payload)
    assert (device_id, product_id) == (DEVICE_A, PRODUCT_ID)
    assert payload["cmd"] == "ATTR_SET_SERVICE"
    assert payload["soundSwitch"] is True
    assert payload["soundAgingType"] == 1
    assert isinstance(payload["msgId"], str) and payload["msgId"]
    assert isinstance(payload["ts"], int)
    assert shadow.get_local_confirmed(DEVICE_A)["soundSwitch"] is True


def test_capability_model_exposes_only_sound_as_writable(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
) -> None:
    """The API makes the exceptional write permission explicit and narrow."""
    client, _, _, _, _ = control_environment

    controls = client.get(f"/api/devices/{DEVICE_A}").json()["controls"]

    assert controls["soundSwitch"]["writable"] is True
    assert controls["soundSwitch"]["cloud_sync_confirmed"] is True
    assert controls["motionDetectionSwitch"]["writable"] is False


def test_control_logs_are_device_scoped() -> None:
    """The dashboard log filter recognizes the controller's device_id field."""
    logs = RingBufferLogHandler()
    logs.setFormatter(logging.Formatter("%(message)s"))
    logs.emit(
        logging.makeLogRecord(
            {"msg": f"CONTROL soundSwitch requested device_id={DEVICE_A}", "levelno": 20}
        )
    )

    assert logs.snapshot()[0]["device_id"] == DEVICE_A


def test_unknown_or_offline_device_never_publishes(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
) -> None:
    """Absent devices fail immediately rather than creating a replayable write."""
    client, harness, _, queue, _ = control_environment

    assert client.patch("/api/devices/UNKNOWN/controls/sound", json={"enabled": True}).status_code == 404
    offline = client.patch(f"/api/devices/{DEVICE_B}/controls/sound", json={"enabled": True})

    assert offline.status_code == 409
    assert harness.published == []
    assert queue.count(DEVICE_B, LOCAL_TO_UPSTREAM) == 0


def test_strict_body_rejects_extra_or_non_boolean_values(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
) -> None:
    """HTTP cannot carry arbitrary fields or coerce arbitrary setting values."""
    client, harness, _, _, _ = control_environment

    assert client.patch(f"/api/devices/{DEVICE_A}/controls/sound", json={"enabled": 1}).status_code == 422
    assert (
        client.patch(
            f"/api/devices/{DEVICE_A}/controls/sound",
            json={"enabled": True, "topic": "arbitrary"},
        ).status_code
        == 422
    )
    assert harness.published == []


def test_ack_is_namespaced_by_device_and_message_id(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
) -> None:
    """Foreign acknowledgements cannot complete the request waiting for device A."""
    client, harness, _, _, _ = control_environment

    harness.ack_device_id = DEVICE_B
    response = client.patch(f"/api/devices/{DEVICE_A}/controls/sound", json={"enabled": True})

    assert response.status_code == 504


def test_wrong_message_id_does_not_confirm_a_control(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
) -> None:
    """A service ACK for another command cannot make this HTTP request succeed."""
    client, harness, _, _, _ = control_environment
    harness.ack_message_id = "not-the-pending-message-id"

    response = client.patch(f"/api/devices/{DEVICE_A}/controls/sound", json={"enabled": True})

    assert response.status_code == 504


def test_second_sound_write_is_rejected_while_first_waits_for_ack(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
) -> None:
    """One device cannot accumulate ambiguous concurrent sound transactions."""
    client, harness, _, _, _ = control_environment
    assert harness.controller is not None
    harness.ack_code = None
    first_finished = threading.Event()

    def wait_for_timeout() -> None:
        """Hold the first request pending long enough to exercise the lock."""
        with pytest.raises(ControlAckTimeoutError):
            harness.controller.set_sound_switch(DEVICE_A, True)
        first_finished.set()

    thread = threading.Thread(target=wait_for_timeout)
    thread.start()
    assert harness.published_event.wait(1.0)
    second = client.patch(f"/api/devices/{DEVICE_A}/controls/sound", json={"enabled": False})
    thread.join()

    assert first_finished.is_set()
    assert second.status_code == 409


@pytest.mark.parametrize(("ack_code", "expected_status"), [(17, 502), (True, 502), (None, 504)])
def test_rejected_or_missing_ack_is_not_success(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
    ack_code: object | None,
    expected_status: int,
) -> None:
    """A non-zero device code and a timeout remain visible to the caller."""
    client, harness, _, _, _ = control_environment
    harness.ack_code = ack_code

    response = client.patch(f"/api/devices/{DEVICE_A}/controls/sound", json={"enabled": True})

    assert response.status_code == expected_status


def test_missing_sound_aging_type_prevents_publication(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
) -> None:
    """Required protocol fields must come from state, never a hard-coded fallback."""
    client, harness, shadow, _, _ = control_environment
    shadow.update_desired(DEVICE_A, {"soundAgingType": None})

    response = client.patch(f"/api/devices/{DEVICE_A}/controls/sound", json={"enabled": True})

    assert response.status_code == 409
    assert harness.published == []


def test_local_confirmation_persists_across_shadow_reopen(tmp_path: Path) -> None:
    """The UI-confirmed value survives a relay restart without becoming cloud desired."""
    database = tmp_path / "shadow.sqlite3"
    shadow = StateShadow(str(database))
    shadow.update_local_confirmed(DEVICE_A, {"soundSwitch": True})
    shadow.close()

    reopened = StateShadow(str(database))
    try:
        assert reopened.get_local_confirmed(DEVICE_A) == {"soundSwitch": True}
        assert reopened.get_desired(DEVICE_A) == {}
    finally:
        reopened.close()


def test_cloud_desired_update_supersedes_local_confirmation(tmp_path: Path) -> None:
    """A later cloud push remains authoritative over a locally confirmed setting."""
    shadow = StateShadow(str(tmp_path / "shadow.sqlite3"))
    try:
        shadow.update_local_confirmed(DEVICE_A, {"soundSwitch": True})
        shadow.update_desired(DEVICE_A, {"soundSwitch": False})

        assert shadow.get_local_confirmed(DEVICE_A) == {}
        assert shadow.get_desired(DEVICE_A)["soundSwitch"] is False
    finally:
        shadow.close()


def test_bridge_keeps_forwarding_a_service_post_after_control_observation(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
    make_config: RelayConfigFactory,
) -> None:
    """The control ACK observer does not consume the device's normal cloud forward."""
    _, harness, _, queue, devices = control_environment
    config = make_config()
    bridge = MqttBridge(config, devices, queue, RelayTelemetry())
    assert harness.controller is not None
    bridge.set_sound_switch_controller(harness.controller)

    class FakeMessage:
        """Minimal Paho message compatible with the local callback."""

        topic = SERVICE_POST_A
        payload = b'{"cmd":"ATTR_SET_SERVICE","msgId":"other","code":0}'
        qos = 0

    bridge._on_local_message(None, None, FakeMessage())  # type: ignore[arg-type]

    assert queue.count(DEVICE_A, LOCAL_TO_UPSTREAM) == 1
