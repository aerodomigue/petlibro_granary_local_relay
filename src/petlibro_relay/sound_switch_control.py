"""The one confirmed interactive control: PLAF203 device sound.

This module deliberately has no generic command, topic, or field API. The
device and cloud behavior of ``soundSwitch`` were verified on a real PLAF203;
other settings have not earned the same privilege and remain read-only.
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

CONTROL_NAME: Final = "soundSwitch"
SUPPORTED_PRODUCT_ID: Final = "PLAF203"
ACK_TIMEOUT_SECONDS: Final = 4.0


@dataclass(frozen=True, slots=True)
class ControlCapability:
    """A deliberately explicit record of a control's proven behavior."""

    writable: bool
    device_ack_confirmed: bool
    cloud_sync_confirmed: bool


CONTROL_CAPABILITIES: Final[dict[str, ControlCapability]] = {
    "soundSwitch": ControlCapability(True, True, True),
    "motionDetectionSwitch": ControlCapability(False, True, False),
}


class ControlError(RuntimeError):
    """Base class for expected, safe device-control failures."""


class ControlOfflineError(ControlError):
    """Raised when a control must not be queued for an absent feeder."""


class ControlStateUnavailableError(ControlError):
    """Raised when a required protocol field is not in the state shadow."""


class ControlBusyError(ControlError):
    """Raised when the same device already has a sound write pending."""


class ControlPublishError(ControlError):
    """Raised when the local MQTT client could not publish immediately."""


class ControlAckTimeoutError(ControlError):
    """Raised when the feeder did not acknowledge a published command in time."""


class ControlAckRejectedError(ControlError):
    """Raised when the feeder explicitly rejects a matching command."""

    def __init__(self, code: object) -> None:
        """Keep the device result code available for the API response."""
        super().__init__(f"Device rejected {CONTROL_NAME} (code={code})")
        self.code = code


@dataclass(slots=True)
class _PendingControl:
    """One in-flight request keyed by device and MQTT message id."""

    value: bool
    sent_at: float
    completed: threading.Event
    code: object | None = None


LocalSoundPublisher = Callable[[str, str, bytes], bool]


class SoundSwitchController:
    """Publish and confirm the only currently supported UI control."""

    def __init__(
        self,
        devices: DeviceManager,
        presence: DevicePresenceTracker,
        shadow: StateShadow,
        publish_local_sound: LocalSoundPublisher,
        ack_timeout_seconds: float = ACK_TIMEOUT_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialize the control service with a narrow local publisher seam.

        Args:
            devices: Source of bridged device contexts and product identities.
            presence: Source of truth for whether the feeder is here now.
            shadow: State needed to reproduce the confirmed command shape.
            publish_local_sound: Publishes this control only, never arbitrary MQTT.
            ack_timeout_seconds: Maximum time to wait for the feeder's `/post` ACK.
            clock: Injectable monotonic wall-clock source for tests.
        """
        self._devices = devices
        self._presence = presence
        self._shadow = shadow
        self._publish_local_sound = publish_local_sound
        self._ack_timeout_seconds = ack_timeout_seconds
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], _PendingControl] = {}
        self._active_devices: set[str] = set()
        self._counters: dict[str, int] = {}

    def capability(self, device_id: str) -> dict[str, Any]:
        """Return safe UI capability state without revealing control internals."""
        context = self._devices.get_by_device_id(device_id)
        desired = self._shadow.get_desired(device_id)
        capability = CONTROL_CAPABILITIES[CONTROL_NAME]
        online = self._presence.is_online(device_id)
        supported = context is not None and context.product_id == SUPPORTED_PRODUCT_ID
        with self._lock:
            pending = device_id in self._active_devices
        required_state_available = isinstance(desired.get("soundAgingType"), int) and isinstance(
            desired.get(CONTROL_NAME), bool
        )
        return {
            "control": CONTROL_NAME,
            "writable": capability.writable and supported,
            "device_ack_confirmed": capability.device_ack_confirmed,
            "cloud_sync_confirmed": capability.cloud_sync_confirmed,
            "device_online": online,
            "required_state_available": required_state_available,
            "pending": pending,
        }

    def set_sound_switch(self, device_id: str, enabled: bool) -> dict[str, Any]:
        """Set device sound locally and wait for its correlated `/post` ACK.

        Args:
            device_id: Device context that must own the pending transaction.
            enabled: Desired boolean state for the one supported setting.

        Returns:
            A safe success projection only after a matching device acknowledgement.

        Raises:
            ControlOfflineError: The feeder is not locally present.
            ControlStateUnavailableError: Required protocol state is missing.
            ControlBusyError: A sound request for this device is already pending.
            ControlPublishError: The local broker could not accept the command.
            ControlAckTimeoutError: No matching feeder acknowledgement arrived.
            ControlAckRejectedError: The feeder returned a non-zero result code.
        """
        context = self._devices.get_by_device_id(device_id)
        if context is None:
            raise ControlStateUnavailableError("Device is not bridged")
        if context.product_id != SUPPORTED_PRODUCT_ID:
            raise ControlStateUnavailableError("Device does not support soundSwitch control")
        if not self._presence.is_online(device_id):
            self._increment("control_rejected")
            raise ControlOfflineError("Device is offline")

        desired = self._shadow.get_desired(device_id)
        sound_aging_type = desired.get("soundAgingType")
        if not isinstance(sound_aging_type, int) or not isinstance(desired.get(CONTROL_NAME), bool):
            self._increment("control_rejected")
            raise ControlStateUnavailableError("Required sound control state is unavailable")

        message_id = uuid.uuid4().hex
        pending = _PendingControl(value=enabled, sent_at=self._clock(), completed=threading.Event())
        key = (device_id, message_id)
        with self._lock:
            if device_id in self._active_devices:
                self._increment_locked("control_rejected")
                raise ControlBusyError("A soundSwitch request is already pending for this device")
            self._active_devices.add(device_id)
            self._pending[key] = pending
            self._increment_locked("control_requests")

        payload = _build_payload(enabled, message_id, sound_aging_type)
        _LOGGER.info(
            "CONTROL soundSwitch requested device_id=%s value=%s msgId=%s cmd=%s",
            device_id,
            enabled,
            message_id,
            protocol.Command.ATTR_SET_SERVICE,
        )
        try:
            if not self._publish_local_sound(device_id, context.product_id, payload):
                self._increment("control_rejected")
                raise ControlPublishError("Local MQTT publication failed")
            if not pending.completed.wait(self._ack_timeout_seconds):
                self._increment("control_timeout")
                _LOGGER.warning("CONTROL soundSwitch timeout device_id=%s msgId=%s", device_id, message_id)
                raise ControlAckTimeoutError("Device acknowledgement timeout")
            if type(pending.code) is not int or pending.code != 0:
                self._increment("control_rejected")
                raise ControlAckRejectedError(pending.code)
            latency_ms = int((self._clock() - pending.sent_at) * 1000)
            self._shadow.update_local_confirmed(device_id, {CONTROL_NAME: enabled})
            self._increment("control_success")
            _LOGGER.info(
                "CONTROL soundSwitch confirmed device_id=%s msgId=%s latency_ms=%d",
                device_id,
                message_id,
                latency_ms,
            )
            return {
                "success": True,
                "device_id": device_id,
                "control": CONTROL_NAME,
                "value": enabled,
                "device_ack": True,
                "cloud_sync_behavior": "confirmed",
            }
        finally:
            with self._lock:
                self._pending.pop(key, None)
                self._active_devices.discard(device_id)

    def observe_device_message(self, device_id: str, topic: str, payload: bytes) -> None:
        """Complete only a matching feeder ACK; routing continues unchanged.

        Args:
            device_id: Device that posted the candidate acknowledgement.
            topic: Local topic, which must be this device's service `/post` topic.
            payload: Raw feeder JSON payload.
        """
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
        capability = self.capability(device_id)
        with self._lock:
            counters = dict(self._counters)
        motion = CONTROL_CAPABILITIES["motionDetectionSwitch"]
        return {
            "soundSwitch": capability,
            "motionDetectionSwitch": {
                "control": "motionDetectionSwitch",
                "writable": motion.writable,
                "device_ack_confirmed": motion.device_ack_confirmed,
                "cloud_sync_confirmed": motion.cloud_sync_confirmed,
            },
            "counters": counters,
        }

    def _increment(self, counter: str) -> None:
        with self._lock:
            self._increment_locked(counter)

    def _increment_locked(self, counter: str) -> None:
        self._counters[counter] = self._counters.get(counter, 0) + 1


def _build_payload(enabled: bool, message_id: str, sound_aging_type: int) -> bytes:
    """Construct the one confirmed PLAF203 sound-switch command format."""
    return json.dumps(
        {
            "cmd": protocol.Command.ATTR_SET_SERVICE,
            "ts": int(time.time() * 1000),
            "msgId": message_id,
            "soundSwitch": enabled,
            "soundAgingType": sound_aging_type,
        },
        separators=(",", ":"),
    ).encode("utf-8")
