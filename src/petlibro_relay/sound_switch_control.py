"""Explicit, feeder-confirmed interactive PLAF203 controls.

This module deliberately exposes no generic MQTT command, topic, or field
API. Only controls whose device behaviour has been confirmed are represented
here. Each request is published only to the local feeder and succeeds only
after its correlated ``/post`` acknowledgement.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from . import protocol
from .device_manager import DeviceManager
from .device_presence import DevicePresenceTracker
from .state_shadow import StateShadow

_LOGGER = logging.getLogger(__name__)

SOUND_CONTROL_NAME: Final = "soundSwitch"
MOTION_CONTROL_NAME: Final = "motionDetectionSwitch"
SUPPORTED_PRODUCT_ID: Final = "PLAF203"
ACK_TIMEOUT_SECONDS: Final = 4.0


@dataclass(frozen=True, slots=True)
class ControlCapability:
    """A deliberately explicit record of a control's proven behaviour."""

    writable: bool
    device_ack_confirmed: bool
    cloud_sync_confirmed: bool


CONTROL_CAPABILITIES: Final[dict[str, ControlCapability]] = {
    SOUND_CONTROL_NAME: ControlCapability(True, True, True),
    MOTION_CONTROL_NAME: ControlCapability(True, True, False),
}


class ControlError(RuntimeError):
    """Base class for expected, safe device-control failures."""


class ControlOfflineError(ControlError):
    """Raised when a control must not be queued for an absent feeder."""


class ControlStateUnavailableError(ControlError):
    """Raised when a required protocol field is not in the state shadow."""


class ControlBusyError(ControlError):
    """Raised when the same device already has an interactive write pending."""


class ControlPublishError(ControlError):
    """Raised when the local MQTT client could not publish immediately."""


class ControlAckTimeoutError(ControlError):
    """Raised when the feeder did not acknowledge a published command in time."""


class ControlAckRejectedError(ControlError):
    """Raised when the feeder explicitly rejects a matching command."""

    def __init__(self, control_name: str, code: object) -> None:
        """Keep the device result code available for the API response."""
        super().__init__(f"Device rejected {control_name} (code={code})")
        self.code = code


@dataclass(slots=True)
class _PendingControl:
    """One in-flight request keyed by device and MQTT message id."""

    control_name: str
    value: bool
    sent_at: float
    completed: threading.Event
    code: object | None = None


LocalControlPublisher = Callable[[str, str, bytes], bool]


class SoundSwitchController:
    """Publish and confirm the two explicitly allowlisted UI controls.

    The historical class name remains for compatibility with the running
    relay. Its public methods are deliberately separate, so HTTP callers can
    never select an arbitrary MQTT field.
    """

    def __init__(
        self,
        devices: DeviceManager,
        presence: DevicePresenceTracker,
        shadow: StateShadow,
        publish_local_control: LocalControlPublisher,
        ack_timeout_seconds: float = ACK_TIMEOUT_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialize the control service with a narrow local publisher seam.

        Args:
            devices: Source of bridged device contexts and product identities.
            presence: Source of truth for whether the feeder is here now.
            shadow: State needed to reproduce the confirmed command shape.
            publish_local_control: Publishes an allowlisted service control only.
            ack_timeout_seconds: Maximum time to wait for the feeder's `/post` ACK.
            clock: Injectable monotonic clock source for tests.
        """
        self._devices = devices
        self._presence = presence
        self._shadow = shadow
        self._publish_local_control = publish_local_control
        self._ack_timeout_seconds = ack_timeout_seconds
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], _PendingControl] = {}
        self._active_devices: set[str] = set()
        self._counters: dict[str, int] = {}

    def capability(self, device_id: str, control_name: str) -> dict[str, Any]:
        """Return safe UI capability state for one explicit control."""
        context = self._devices.get_by_device_id(device_id)
        desired = self._shadow.get_desired(device_id)
        definition = CONTROL_CAPABILITIES[control_name]
        online = self._presence.is_online(device_id)
        supported = context is not None and context.product_id == SUPPORTED_PRODUCT_ID
        with self._lock:
            pending = device_id in self._active_devices
        return {
            "control": control_name,
            "writable": definition.writable and supported,
            "device_ack_confirmed": definition.device_ack_confirmed,
            "cloud_sync_confirmed": definition.cloud_sync_confirmed,
            "device_online": online,
            "required_state_available": _required_state_available(control_name, desired),
            "pending": pending,
        }

    def set_sound_switch(self, device_id: str, enabled: bool) -> dict[str, Any]:
        """Set device sound locally and wait for its correlated `/post` ACK."""
        return self._set_control(device_id, enabled, SOUND_CONTROL_NAME)

    def set_motion_detection_switch(self, device_id: str, enabled: bool) -> dict[str, Any]:
        """Set motion detection locally and wait for its correlated `/post` ACK."""
        return self._set_control(device_id, enabled, MOTION_CONTROL_NAME)

    def observe_device_message(self, device_id: str, topic: str, payload: bytes) -> None:
        """Complete only a matching feeder ACK; normal bridge routing continues."""
        if topic != f"{protocol.topic_prefix(device_id)}/device/service/post":
            return
        try:
            body = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(body, dict) or body.get("cmd") != protocol.Command.ATTR_SET_SERVICE:
            return
        message_id = body.get("msgId")
        if not isinstance(message_id, str):
            return
        with self._lock:
            pending = self._pending.get((device_id, message_id))
            if pending is None:
                return
            pending.code = body.get("code")
            pending.completed.set()

    def snapshot(self, device_id: str) -> dict[str, Any]:
        """Return safe per-device control telemetry for the dashboard/API."""
        with self._lock:
            counters = dict(self._counters)
        return {
            control_name: self.capability(device_id, control_name)
            for control_name in CONTROL_CAPABILITIES
        } | {"counters": counters}

    def _set_control(self, device_id: str, enabled: bool, control_name: str) -> dict[str, Any]:
        """Publish one allowlisted setting and await a device-scoped ACK."""
        context = self._devices.get_by_device_id(device_id)
        if context is None:
            raise ControlStateUnavailableError("Device is not bridged")
        if context.product_id != SUPPORTED_PRODUCT_ID:
            raise ControlStateUnavailableError(f"Device does not support {control_name} control")
        if not self._presence.is_online(device_id):
            self._increment("control_rejected")
            raise ControlOfflineError("Device is offline")

        desired = self._shadow.get_desired(device_id)
        if not _required_state_available(control_name, desired):
            self._increment("control_rejected")
            raise ControlStateUnavailableError(f"Required {control_name} control state is unavailable")

        message_id = uuid.uuid4().hex
        pending = _PendingControl(
            control_name=control_name,
            value=enabled,
            sent_at=self._clock(),
            completed=threading.Event(),
        )
        key = (device_id, message_id)
        with self._lock:
            if device_id in self._active_devices:
                self._increment_locked("control_rejected")
                raise ControlBusyError(f"A control request is already pending for device {device_id}")
            self._active_devices.add(device_id)
            self._pending[key] = pending
            self._increment_locked("control_requests")

        payload = _build_payload(control_name, enabled, message_id, desired)
        _LOGGER.info(
            "CONTROL %s requested device_id=%s value=%s msgId=%s cmd=%s",
            control_name,
            device_id,
            enabled,
            message_id,
            protocol.Command.ATTR_SET_SERVICE,
        )
        try:
            if not self._publish_local_control(device_id, context.product_id, payload):
                self._increment("control_rejected")
                raise ControlPublishError("Local MQTT publication failed")
            if not pending.completed.wait(self._ack_timeout_seconds):
                self._increment("control_timeout")
                _LOGGER.warning("CONTROL %s timeout device_id=%s msgId=%s", control_name, device_id, message_id)
                raise ControlAckTimeoutError("Device acknowledgement timeout")
            if type(pending.code) is not int or pending.code != 0:
                self._increment("control_rejected")
                raise ControlAckRejectedError(control_name, pending.code)
            latency_ms = int((self._clock() - pending.sent_at) * 1000)
            self._shadow.update_local_confirmed(device_id, {control_name: enabled})
            self._increment("control_success")
            _LOGGER.info(
                "CONTROL %s confirmed device_id=%s msgId=%s latency_ms=%d",
                control_name,
                device_id,
                message_id,
                latency_ms,
            )
            return {
                "success": True,
                "device_id": device_id,
                "control": control_name,
                "value": enabled,
                "device_ack": True,
                "cloud_sync_behavior": (
                    "confirmed" if CONTROL_CAPABILITIES[control_name].cloud_sync_confirmed else "unknown"
                ),
            }
        finally:
            with self._lock:
                self._pending.pop(key, None)
                self._active_devices.discard(device_id)

    def _increment(self, counter: str) -> None:
        with self._lock:
            self._increment_locked(counter)

    def _increment_locked(self, counter: str) -> None:
        self._counters[counter] = self._counters.get(counter, 0) + 1


def _required_state_available(control_name: str, desired: dict[str, Any]) -> bool:
    """Return whether the shadow contains fields needed to build a control."""
    if control_name == SOUND_CONTROL_NAME:
        return isinstance(desired.get("soundAgingType"), int) and isinstance(
            desired.get(SOUND_CONTROL_NAME), bool
        )
    if control_name == MOTION_CONTROL_NAME:
        return isinstance(desired.get(MOTION_CONTROL_NAME), bool)
    return False


def _build_payload(
    control_name: str, enabled: bool, message_id: str, desired: dict[str, Any]
) -> bytes:
    """Construct the exact proven MQTT payload for one explicit setting."""
    payload: dict[str, object] = {
        "cmd": protocol.Command.ATTR_SET_SERVICE,
        "ts": int(time.time() * 1000),
        "msgId": message_id,
        control_name: enabled,
    }
    if control_name == SOUND_CONTROL_NAME:
        sound_aging_type = desired["soundAgingType"]
        if not isinstance(sound_aging_type, int):
            raise ControlStateUnavailableError("Required soundSwitch control state is unavailable")
        payload["soundAgingType"] = sound_aging_type
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")
