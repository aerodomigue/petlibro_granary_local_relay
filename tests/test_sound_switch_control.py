"""Safe mock-only coverage for explicitly confirmed feeder controls."""

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
from petlibro_relay.delivery_pump import DeliveryPump, PumpTarget
from petlibro_relay.device_manager import DeviceManager
from petlibro_relay.device_presence import DevicePresenceTracker
from petlibro_relay.device_registry import DeviceIdentity, DeviceRegistry
from petlibro_relay.message_queue import MessageQueue
from petlibro_relay.mqtt_bridge import MqttBridge
from petlibro_relay.observability.log_buffer import RingBufferLogHandler
from petlibro_relay.observability.telemetry import RelayTelemetry
from petlibro_relay.state_cache import StateCache
from petlibro_relay.state_shadow import StateShadow
from petlibro_relay.sound_switch_control import (
    ControlAckTimeoutError,
    ScheduleResyncResult,
    SoundSwitchController,
)
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
            ack_body = {
                "cmd": body["cmd"],
                "msgId": self.ack_message_id or body["msgId"],
                "code": self.ack_code,
            }
            for control_name in ("soundSwitch", "motionDetectionSwitch"):
                if control_name in body:
                    ack_body[control_name] = body[control_name]
            ack = json.dumps(ack_body).encode()
            self.controller.observe_device_message(
                self.ack_device_id or device_id,
                f"dl/{product_id}/{self.ack_device_id or device_id}/device/service/post",
                ack,
            )
        return True


class RecordingMqttClient:
    """Fake connected MQTT client used to prove bridge routing without a broker."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []
        self.published_event = threading.Event()

    def is_connected(self) -> bool:
        """Represent a connected fake MQTT session."""
        return True

    def publish(self, topic: str, payload: bytes, qos: int = 0) -> Any:
        """Record one fake publication and synchronously confirm it."""
        self.published.append((topic, payload))
        self.published_event.set()

        class Published:
            """Minimal Paho publication result."""

            rc = 0

            def wait_for_publish(self, timeout: float | None = None) -> None:
                """Confirm immediately without a network."""

            def is_published(self) -> bool:
                """Report the fake publication as confirmed."""
                return True

        return Published()


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
    shadow.update_desired(
        DEVICE_A,
        {"soundSwitch": False, "soundAgingType": 1, "motionDetectionSwitch": False},
    )
    shadow.update_desired(
        DEVICE_B,
        {"soundSwitch": False, "soundAgingType": 1, "motionDetectionSwitch": False},
    )
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


def test_manual_dispense_is_ack_confirmed_and_never_queued(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
) -> None:
    """Manual dispensing uses one immediate local command, not durable replay."""
    client, harness, _, queue, _ = control_environment

    response = client.post(f"/api/devices/{DEVICE_A}/dispense", json={"grainNum": 2})

    assert response.status_code == 200
    payload = json.loads(harness.published[-1][2])
    assert payload["cmd"] == "MANUAL_FEEDING_SERVICE"
    assert payload["grainNum"] == 2
    assert isinstance(payload["msgId"], str) and payload["msgId"]
    assert isinstance(payload["ts"], int)
    assert response.json()["device_ack"] is True
    assert queue.count(DEVICE_A, LOCAL_TO_UPSTREAM) == 0


def test_manual_dispense_rejects_an_offline_or_invalid_request(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
) -> None:
    """A manual action must never be deferred until an absent feeder reconnects."""
    client, harness, _, queue, _ = control_environment

    assert client.post(f"/api/devices/{DEVICE_A}/dispense", json={"grainNum": 0}).status_code == 422
    offline = client.post(f"/api/devices/{DEVICE_B}/dispense", json={"grainNum": 1})

    assert offline.status_code == 409
    assert harness.published == []
    assert queue.count(DEVICE_B, LOCAL_TO_UPSTREAM) == 0


@pytest.mark.parametrize(("ack_code", "expected_status"), [(7, 502), (None, 504)])
def test_manual_dispense_requires_a_successful_feeder_ack(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
    ack_code: object | None,
    expected_status: int,
) -> None:
    """Manual dispense never reports success solely because local publish succeeded."""
    client, harness, _, queue, _ = control_environment
    assert harness.controller is not None
    harness.ack_code = ack_code
    if ack_code is None:
        harness.controller._ack_timeout_seconds = 0.01

    response = client.post(f"/api/devices/{DEVICE_A}/dispense", json={"grainNum": 2})

    assert response.status_code == expected_status
    assert queue.count(DEVICE_A, LOCAL_TO_UPSTREAM) == 0


def test_capability_model_exposes_only_explicitly_confirmed_controls_as_writable(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
) -> None:
    """The API exposes only its small, explicit write allowlist."""
    client, _, _, _, _ = control_environment

    controls = client.get(f"/api/devices/{DEVICE_A}/daily").json()["controls"]

    assert controls["soundSwitch"]["writable"] is True
    assert controls["soundSwitch"]["cloud_sync_confirmed"] is True
    assert controls["motionDetectionSwitch"]["writable"] is True
    assert controls["motionDetectionSwitch"]["cloud_sync_confirmed"] is False


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


def test_motion_control_builds_only_the_confirmed_payload_and_waits_for_ack(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
) -> None:
    """Motion detection uses only its fields proven by the feeder experiment."""
    client, harness, shadow, _, _ = control_environment

    response = client.patch(f"/api/devices/{DEVICE_A}/controls/motion", json={"enabled": True})

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "device_id": DEVICE_A,
        "control": "motionDetectionSwitch",
        "value": True,
        "device_ack": True,
        "cloud_sync_behavior": "unknown",
    }
    assert len(harness.published) == 1
    payload = json.loads(harness.published[0][2])
    assert payload["cmd"] == "ATTR_SET_SERVICE"
    assert payload["motionDetectionSwitch"] is True
    assert set(payload) == {"cmd", "ts", "msgId", "motionDetectionSwitch"}
    assert shadow.get_local_confirmed(DEVICE_A)["motionDetectionSwitch"] is True


def test_motion_route_is_strict_and_offline_never_queues(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
) -> None:
    """Motion endpoint accepts only its typed body and never creates a replay write."""
    client, harness, _, queue, _ = control_environment

    assert client.patch(f"/api/devices/{DEVICE_A}/controls/motion", json={"enabled": 1}).status_code == 422
    assert client.patch(
        f"/api/devices/{DEVICE_A}/controls/motion",
        json={"enabled": True, "motionDetectionAgingType": 1},
    ).status_code == 200
    harness.published.clear()
    offline = client.patch(f"/api/devices/{DEVICE_B}/controls/motion", json={"enabled": True})

    assert offline.status_code == 409
    assert client.patch("/api/devices/UNKNOWN/controls/motion", json={"enabled": True}).status_code == 404
    assert harness.published == []
    assert queue.count(DEVICE_B, LOCAL_TO_UPSTREAM) == 0


def test_motion_ack_is_namespaced_and_nonzero_code_is_not_success(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
) -> None:
    """Motion uses the same `(device_id, msgId)` ACK boundary as sound."""
    client, harness, _, _, _ = control_environment
    harness.ack_device_id = DEVICE_B

    foreign_ack = client.patch(f"/api/devices/{DEVICE_A}/controls/motion", json={"enabled": True})
    assert foreign_ack.status_code == 504

    harness.ack_device_id = None
    harness.ack_code = 7
    rejected = client.patch(f"/api/devices/{DEVICE_A}/controls/motion", json={"enabled": True})
    assert rejected.status_code == 502


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


def test_cloud_desired_delta_does_not_erase_local_confirmation(tmp_path: Path) -> None:
    """Local feeder ACK stays visible if a divergent cloud value arrives later."""
    shadow = StateShadow(str(tmp_path / "shadow.sqlite3"))
    try:
        shadow.update_local_confirmed(DEVICE_A, {"soundSwitch": True})
        shadow.update_desired(DEVICE_A, {"soundSwitch": False})

        assert shadow.get_local_confirmed(DEVICE_A)["soundSwitch"] is True
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


def test_local_attr_set_ack_resolves_then_forwards_once_when_online(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
    make_config: RelayConfigFactory,
) -> None:
    """A locally issued control stays local on `/sub`, but its ACK reaches cloud once."""
    _, harness, _, queue, devices = control_environment
    assert harness.controller is not None
    bridge = MqttBridge(make_config(), devices, queue, RelayTelemetry())
    local_client = RecordingMqttClient()
    upstream_client = RecordingMqttClient()
    bridge._local_client = local_client  # type: ignore[assignment]
    bridge.set_sound_switch_controller(harness.controller)
    harness.controller._publish_local_control = bridge.publish_sound_switch

    context = devices.get_by_device_id(DEVICE_A)
    assert context is not None
    context.telemetry.upstream_online()
    result: dict[str, object] = {}

    def submit_control() -> None:
        """Run the ACK-waiting write while the test injects its feeder response."""
        result.update(harness.controller.set_sound_switch(DEVICE_A, True))

    worker = threading.Thread(target=submit_control)
    worker.start()
    assert local_client.published_event.wait(1.0)
    assert queue.count(DEVICE_A, LOCAL_TO_UPSTREAM) == 0


    control_topic, control_payload = local_client.published[0]
    request = json.loads(control_payload)
    assert control_topic == f"dl/{PRODUCT_ID}/{DEVICE_A}/device/service/sub"
    assert request["cmd"] == "ATTR_SET_SERVICE"

    ack_payload = json.dumps(
        {
            "cmd": "ATTR_SET_SERVICE",
            "msgId": request["msgId"],
            "code": 0,
            "soundSwitch": True,
        }
    ).encode()

    class FeederAck:
        """Minimal local broker message for the feeder's natural ACK."""

        topic = SERVICE_POST_A
        payload = ack_payload
        qos = 0

    bridge._on_local_message(None, None, FeederAck())  # type: ignore[arg-type]
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert result["success"] is True
    queued = queue.peek_oldest(DEVICE_A, LOCAL_TO_UPSTREAM)
    assert queued is not None
    assert queued.payload == ack_payload
    assert queued.is_live is True

    pump = DeliveryPump(
        LOCAL_TO_UPSTREAM,
        queue,
        targets=lambda: [PumpTarget(DEVICE_A, upstream_client, context.telemetry)],  # type: ignore[arg-type]
        is_cloud_to_device=False,
        replay_rate_per_device=1.0,
        replay_rate_global=1.0,
    )
    assert pump._run_once() is True
    assert upstream_client.published == [(SERVICE_POST_A, ack_payload)]
    assert queue.count(DEVICE_A, LOCAL_TO_UPSTREAM) == 0


def test_manual_dispense_ack_forwards_once_without_forwarding_the_command(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
    make_config: RelayConfigFactory,
) -> None:
    """The feeder's natural manual-feed ACK is the sole cloud-visible message."""
    _, harness, _, queue, devices = control_environment
    assert harness.controller is not None
    harness.ack_code = None
    bridge = MqttBridge(make_config(), devices, queue, RelayTelemetry())
    local_client = RecordingMqttClient()
    upstream_client = RecordingMqttClient()
    bridge._local_client = local_client  # type: ignore[assignment]
    bridge.set_sound_switch_controller(harness.controller)
    harness.controller._publish_local_control = bridge.publish_sound_switch
    context = devices.get_by_device_id(DEVICE_A)
    assert context is not None
    context.telemetry.upstream_online()
    outcome: dict[str, object] = {}

    worker = threading.Thread(
        target=lambda: outcome.update(harness.controller.dispense(DEVICE_A, 2))
    )
    worker.start()
    assert local_client.published_event.wait(1.0)
    command_topic, command_payload = local_client.published[0]
    command = json.loads(command_payload)
    assert command_topic.endswith("/device/service/sub")
    assert command["cmd"] == "MANUAL_FEEDING_SERVICE"
    assert queue.count(DEVICE_A, LOCAL_TO_UPSTREAM) == 0
    assert upstream_client.published == []

    ack_payload = json.dumps(
        {"cmd": "MANUAL_FEEDING_SERVICE", "msgId": command["msgId"], "code": 0}
    ).encode()

    class FeederAck:
        """Natural feeder service-post acknowledgement for the manual command."""

        topic = SERVICE_POST_A
        payload = ack_payload
        qos = 0

    bridge._on_local_message(None, None, FeederAck())  # type: ignore[arg-type]
    worker.join(timeout=1.0)

    assert outcome["success"] is True
    queued = queue.peek_oldest(DEVICE_A, LOCAL_TO_UPSTREAM)
    assert queued is not None and queued.payload == ack_payload
    pump = DeliveryPump(
        LOCAL_TO_UPSTREAM,
        queue,
        targets=lambda: [PumpTarget(DEVICE_A, upstream_client, context.telemetry)],  # type: ignore[arg-type]
        is_cloud_to_device=False,
    )
    assert pump._run_once() is True
    assert upstream_client.published == [(SERVICE_POST_A, ack_payload)]


def test_schedule_resync_ack_is_never_forwarded_upstream(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
    make_config: RelayConfigFactory,
) -> None:
    """A reconnect resync uses the same local-only ACK boundary as schedule edits."""
    _, harness, shadow, queue, devices = control_environment
    assert harness.controller is not None
    bridge = MqttBridge(make_config(), devices, queue, RelayTelemetry())
    local_client = RecordingMqttClient()
    bridge._local_client = local_client  # type: ignore[assignment]
    bridge.set_sound_switch_controller(harness.controller)
    harness.controller._publish_local_control = bridge.publish_sound_switch
    local_plan = {
        "planId": -1,
        "executionTime": "08:00",
        "grainNum": 1,
        "enableAudio": False,
        "audioTimes": 1,
        "repeatDay": [1, 2, 3, 4, 5, 6, 7],
        "syncTime": 1_786_659_757_000,
    }
    cloud_plan = {**local_plan, "planId": 42, "executionTime": "18:00"}
    never_plan = {**local_plan, "planId": -2, "executionTime": "12:00", "repeatDay": []}
    shadow.replace_local_schedule_plans(DEVICE_A, [local_plan, never_plan, cloud_plan])
    result: list[ScheduleResyncResult | None] = []

    def resync_schedule() -> None:
        """Run the reconnect restoration while the test injects its feeder ACK."""
        result.append(harness.controller.resync_persisted_schedules(DEVICE_A))

    worker = threading.Thread(target=resync_schedule)
    worker.start()
    assert local_client.published_event.wait(1.0)
    _, control_payload = local_client.published[0]
    request = json.loads(control_payload)
    assert request["cmd"] == "FEEDING_PLAN_SERVICE"
    assert {plan["planId"] for plan in request["plans"]} == {-2, -1, 42}
    assert next(plan for plan in request["plans"] if plan["planId"] == -2)["repeatDay"] == []
    ack_payload = json.dumps(
        {"cmd": "FEEDING_PLAN_SERVICE", "msgId": request["msgId"], "code": 0}
    ).encode()

    class FeederAck:
        """Minimal local broker message for the schedule acknowledgement."""

        topic = SERVICE_POST_A
        payload = ack_payload
        qos = 0

    bridge._on_local_message(None, None, FeederAck())  # type: ignore[arg-type]
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert result
    outcome = result[0]
    assert outcome is not None
    assert outcome.plans_total == 3
    assert outcome.cloud_plans == 1
    assert outcome.local_plans == 2
    assert queue.count(DEVICE_A, LOCAL_TO_UPSTREAM) == 0


def test_offline_sound_ack_is_queued_and_replayed_without_reissuing_the_control(
    control_environment: tuple[TestClient, ControlHarness, StateShadow, MessageQueue, DeviceManager],
    make_config: RelayConfigFactory,
) -> None:
    """A locally confirmed sound change later forwards only the feeder's natural ACK."""
    _, harness, _, queue, devices = control_environment
    assert harness.controller is not None
    harness.ack_code = None
    bridge = MqttBridge(make_config(), devices, queue, RelayTelemetry())
    bridge.set_sound_switch_controller(harness.controller)
    result: dict[str, object] = {}

    def set_sound() -> None:
        """Issue the interactive command while the simulated cloud is offline."""
        result.update(harness.controller.set_sound_switch(DEVICE_A, True))

    worker = threading.Thread(target=set_sound)
    worker.start()
    assert harness.published_event.wait(1.0)
    request_payload = json.loads(harness.published[0][2])
    ack_payload = json.dumps(
        {
            "cmd": "ATTR_SET_SERVICE",
            "msgId": request_payload["msgId"],
            "code": 0,
            "soundSwitch": True,
        }
    ).encode()

    class FakeMessage:
        """Minimal Paho message carrying the natural feeder acknowledgement."""

        topic = SERVICE_POST_A
        payload = ack_payload
        qos = 0

    bridge._on_local_message(None, None, FakeMessage())  # type: ignore[arg-type]
    worker.join(timeout=1.0)

    assert result["success"] is True
    assert queue.count(DEVICE_A, LOCAL_TO_UPSTREAM) == 1
    queued = queue.peek_oldest(DEVICE_A, LOCAL_TO_UPSTREAM)
    assert queued is not None
    assert queued.payload == ack_payload
    assert queued.is_live is False

    class FakePublishResult:
        """Synchronously confirms a fake upstream publication."""

        rc = 0

        def wait_for_publish(self, timeout: float | None = None) -> None:
            """Finish immediately without using a network."""

        def is_published(self) -> bool:
            """Report that the fake client accepted the packet."""
            return True

    class FakeUpstreamClient:
        """Fake reconnected upstream session recording only replay payloads."""

        def __init__(self) -> None:
            self.published: list[tuple[str, bytes]] = []

        def is_connected(self) -> bool:
            """Represent an MQTT session that has received CONNACK 0."""
            return True

        def publish(self, topic: str, payload: bytes, qos: int = 0) -> FakePublishResult:
            """Record a replay attempt without opening a socket."""
            self.published.append((topic, payload))
            return FakePublishResult()

    fake_upstream = FakeUpstreamClient()
    context = devices.get_by_device_id(DEVICE_A)
    assert context is not None
    clock = [0.0]
    pump = DeliveryPump(
        LOCAL_TO_UPSTREAM,
        queue,
        targets=lambda: [PumpTarget(DEVICE_A, fake_upstream, context.telemetry)],  # type: ignore[arg-type]
        is_cloud_to_device=False,
        replay_rate_per_device=1.0,
        replay_rate_global=1.0,
        replay_start_delay_seconds=1.0,
        replay_jitter=0.0,
        clock=lambda: clock[0],
        random_source=lambda: 0.5,
    )

    assert pump._run_once() is False
    assert fake_upstream.published == []
    clock[0] = 1.0
    assert pump._run_once() is True
    assert fake_upstream.published == [(SERVICE_POST_A, ack_payload)]
    assert queue.count(DEVICE_A, LOCAL_TO_UPSTREAM) == 0
