"""Explicit, feeder-confirmed PLAF203 settings and local schedule controls.

No HTTP route can select arbitrary MQTT fields. Every caller uses one of the
allowlisted group builders below, publication is local only, and success is
reported only after the feeder posts a matching MQTT acknowledgement.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time as LocalTime
from enum import Enum, auto
from typing import Any, Final
from zoneinfo import ZoneInfo

from . import protocol
from .device_manager import DeviceManager
from .device_presence import DevicePresenceTracker
from .state_shadow import StateShadow

_LOGGER = logging.getLogger(__name__)

SOUND_CONTROL_NAME: Final = "soundSwitch"
MOTION_CONTROL_NAME: Final = "motionDetectionSwitch"
SOUND_DETECTION_CONTROL_NAME: Final = "soundDetectionSwitch"
LIGHT_CONTROL_NAME: Final = "lightSwitch"
CAMERA_CONTROL_NAME: Final = "cameraSwitch"
VIDEO_CONTROL_NAME: Final = "videoRecordSwitch"
FEEDING_VIDEO_CONTROL_NAME: Final = "feedingVideoSwitch"
BOWL_CONTROL_NAME: Final = "bowlMode"
SUPPORTED_PRODUCT_ID: Final = "PLAF203"
ACK_TIMEOUT_SECONDS: Final = 4.0
MILLISECONDS_PER_SECOND: Final = 1000
MAX_VOLUME: Final = 100
MIN_VIDEO_MINUTES: Final = 1
MAX_VIDEO_MINUTES: Final = 5
MIN_GRAIN_NUM: Final = 1
MAX_GRAIN_NUM: Final = 48
MIN_AUDIO_TIMES: Final = 1
MAX_AUDIO_TIMES: Final = 5

GROUP_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "motion": (
        "motionDetectionSwitch", "motionDetectionAgingType", "motionDetectionStartTime",
        "motionDetectionEndTime", "motionDetectionStartTimeUtc", "motionDetectionEndTimeUtc",
        "motionDetectionSensitivity", "motionDetectionRange",
    ),
    "sound_detection": (
        "soundDetectionSwitch", "soundDetectionAgingType", "soundDetectionStartTime",
        "soundDetectionEndTime", "soundDetectionStartTimeUtc", "soundDetectionEndTimeUtc",
        "soundDetectionSensitivity",
    ),
    "sound": (
        "soundSwitch", "soundAgingType", "soundStartTime", "soundEndTime", "soundStartTimeUtc",
        "soundEndTimeUtc", "soundTimes", "volume",
    ),
    "light": (
        "lightSwitch", "lightAgingType", "lightingStartTime", "lightingEndTime",
        "lightingStartTimeUtc", "lightingEndTimeUtc", "lightingTimes", "filterLedSwitch",
    ),
    "camera": (
        "cameraSwitch", "cameraAgingType", "cameraStartTime", "cameraEndTime",
        "cameraStartTimeUtc", "cameraEndTimeUtc", "resolution", "nightVision",
    ),
    "video": (
        "videoRecordSwitch", "videoRecordMode", "videoRecordAgingType", "videoRecordStartTime",
        "videoRecordEndTime", "videoRecordStartTimeUtc", "videoRecordEndTimeUtc",
        "videoWatermarkSwitch",
    ),
    "feeding_video": (
        "feedingVideoSwitch", "enableVideoStartFeedingPlan", "beforeFeedingPlanTime",
        "automaticRecording", "enableVideoAfterManualFeeding", "afterManualFeedingTime",
    ),
    "bowl": ("bowlMode",),
}


@dataclass(frozen=True, slots=True)
class ControlCapability:
    """Explicitly records a supported local setting group."""

    writable: bool
    device_ack_confirmed: bool
    cloud_sync_confirmed: bool


CONTROL_CAPABILITIES: Final[dict[str, ControlCapability]] = {
    SOUND_CONTROL_NAME: ControlCapability(True, True, True),
    MOTION_CONTROL_NAME: ControlCapability(True, True, False),
    SOUND_DETECTION_CONTROL_NAME: ControlCapability(True, True, False),
    LIGHT_CONTROL_NAME: ControlCapability(True, True, False),
    CAMERA_CONTROL_NAME: ControlCapability(True, True, False),
    VIDEO_CONTROL_NAME: ControlCapability(True, True, False),
    FEEDING_VIDEO_CONTROL_NAME: ControlCapability(True, True, False),
    BOWL_CONTROL_NAME: ControlCapability(True, True, False),
}


class ControlError(RuntimeError):
    """Base class for expected, safe device-control failures."""


class ControlOfflineError(ControlError):
    """Raised when an interactive operation would target an absent feeder."""


class ControlStateUnavailableError(ControlError):
    """Raised when a builder cannot safely reconstruct a required value."""


class ControlBusyError(ControlError):
    """Raised when one device already has a correlated operation in flight."""


class ControlPublishError(ControlError):
    """Raised when the local MQTT client cannot publish immediately."""


class ControlAckTimeoutError(ControlError):
    """Raised when the feeder did not acknowledge the local command."""


class ControlAckRejectedError(ControlError):
    """Raised when the feeder explicitly rejects a matching command."""

    def __init__(self, control_name: str, code: object) -> None:
        super().__init__(f"Device rejected {control_name} (code={code})")
        self.code = code


class LocalControlAckRoute(Enum):
    """Whether a matched local-control ACK may continue to PETLIBRO."""

    NOT_MATCHED = auto()
    FORWARD_TO_CLOUD = auto()
    LOCAL_ONLY = auto()


@dataclass(slots=True)
class _PendingControl:
    """One transaction, always namespaced by device id and msgId."""

    command: str
    control_name: str
    values: dict[str, Any]
    sent_at: float
    completed: threading.Event
    code: object | None = None


@dataclass(frozen=True, slots=True)
class ScheduleResyncResult:
    """A feeder-confirmed persisted schedule resynchronization."""

    device_id: str
    plans_total: int
    cloud_plans: int
    local_plans: int


LocalControlPublisher = Callable[[str, str, bytes], bool]


class DeviceControlController:
    """Build, publish and ACK explicit local settings and feeding schedules."""

    def __init__(
        self,
        devices: DeviceManager,
        presence: DevicePresenceTracker,
        shadow: StateShadow,
        publish_local_control: LocalControlPublisher,
        timezone_name: str = "UTC",
        ack_timeout_seconds: float = ACK_TIMEOUT_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._devices = devices
        self._presence = presence
        self._shadow = shadow
        self._publish_local_control = publish_local_control
        self._timezone = ZoneInfo(timezone_name)
        self._ack_timeout_seconds = ack_timeout_seconds
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], _PendingControl] = {}
        self._active_devices: set[str] = set()
        self._counters: dict[str, int] = {}

    def capability(self, device_id: str, control_name: str) -> dict[str, Any]:
        """Return safe UI capability state for one explicit control."""
        context = self._devices.get_by_device_id(device_id)
        desired = self._effective_settings(device_id)
        definition = CONTROL_CAPABILITIES[control_name]
        with self._lock:
            pending = device_id in self._active_devices
        return {
            "control": control_name,
            "writable": definition.writable and context is not None and context.product_id == SUPPORTED_PRODUCT_ID,
            "device_ack_confirmed": definition.device_ack_confirmed,
            "cloud_sync_confirmed": definition.cloud_sync_confirmed,
            "device_online": self._presence.is_online(device_id),
            "required_state_available": isinstance(desired.get(control_name), bool)
            if control_name != BOWL_CONTROL_NAME else isinstance(desired.get(control_name), str),
            "pending": pending,
        }

    def snapshot(self, device_id: str) -> dict[str, Any]:
        """Return all explicit capabilities plus transaction counters."""
        with self._lock:
            counters = dict(self._counters)
        return {name: self.capability(device_id, name) for name in CONTROL_CAPABILITIES} | {"counters": counters}

    def set_sound_switch(self, device_id: str, enabled: bool) -> dict[str, Any]:
        """Keep the original narrow sound endpoint fully compatible."""
        desired = self._effective_settings(device_id)
        aging_type = desired.get("soundAgingType")
        if not isinstance(aging_type, int):
            raise ControlStateUnavailableError("Required soundSwitch control state is unavailable")
        response = self.set_group(
            device_id, "sound", {"soundSwitch": enabled, "soundAgingType": aging_type}, minimal=True
        )
        response["control"] = SOUND_CONTROL_NAME
        response["value"] = enabled
        response["cloud_sync_behavior"] = "confirmed"
        return response

    def set_motion_detection_switch(self, device_id: str, enabled: bool) -> dict[str, Any]:
        """Keep the proven minimal motion switch packet fully compatible."""
        response = self.set_group(device_id, "motion", {"motionDetectionSwitch": enabled}, minimal=True)
        response["control"] = MOTION_CONTROL_NAME
        response["value"] = enabled
        return response

    def set_group(
        self, device_id: str, group_name: str, updates: dict[str, Any], minimal: bool = False
    ) -> dict[str, Any]:
        """Apply a validated settings group through one ATTR_SET_SERVICE ACK."""
        allowed = GROUP_FIELDS[group_name]
        if not updates or any(key not in allowed for key in updates):
            raise ControlStateUnavailableError("Unsupported control fields")
        values = dict(updates) if minimal else {
            key: value for key, value in self._effective_settings(device_id).items() if key in allowed
        }
        values.update(updates)
        _populate_time_fields(values, self._timezone)
        _populate_duration_fields(values)
        control_name = next(iter(updates))
        response = self._submit(
            device_id,
            protocol.Command.ATTR_SET_SERVICE,
            control_name,
            values,
            {"cmd": protocol.Command.ATTR_SET_SERVICE, "ts": _timestamp_ms(), **values},
        )
        self._shadow.update_local_confirmed(device_id, values)
        return response

    def create_schedule(self, device_id: str, requested: dict[str, Any]) -> dict[str, Any]:
        """Create a device-local plan using the next persistent negative ID."""
        plans = self._schedule_plans(device_id)
        plan = dict(requested)
        plan["planId"] = _next_local_plan_id(plans)
        plan["syncTime"] = _sync_time_ms()
        _validate_plan(plan)
        _reject_time_collision(plans, plan, ignored_plan_id=None)
        return self._submit_schedule(device_id, plans + [plan], "schedule:create")

    def update_schedule(self, device_id: str, plan_id: int, requested: dict[str, Any]) -> dict[str, Any]:
        """Modify an existing plan while retaining its ID and rebuilding the snapshot."""
        plans = self._schedule_plans(device_id)
        existing = next((plan for plan in plans if plan["planId"] == plan_id), None)
        if existing is None:
            raise ControlStateUnavailableError("Schedule plan is not known")
        updated = {**existing, **requested, "planId": plan_id, "syncTime": _sync_time_ms()}
        _validate_plan(updated)
        _reject_time_collision(plans, updated, ignored_plan_id=plan_id)
        return self._submit_schedule(
            device_id, [updated if plan["planId"] == plan_id else plan for plan in plans], "schedule:update"
        )

    def delete_schedule(self, device_id: str, plan_id: int) -> dict[str, Any]:
        """Delete a plan by submitting the remaining complete snapshot."""
        plans = self._schedule_plans(device_id)
        remaining = [plan for plan in plans if plan["planId"] != plan_id]
        if len(remaining) == len(plans):
            raise ControlStateUnavailableError("Schedule plan is not known")
        return self._submit_schedule(device_id, remaining, "schedule:delete")

    def resync_persisted_schedules(self, device_id: str) -> ScheduleResyncResult | None:
        """Restore all known schedules to a just-reconnected feeder.

        This deliberately reads only ``schedule_plans``.  An empty table is
        unknown state, not proof that the feeder should receive an empty
        snapshot, so no MQTT publication occurs in that case.
        """
        plans = self._persisted_schedule_plans(device_id)
        if not plans:
            _LOGGER.info("SCHEDULE RESYNC SKIP device=%s reason=no_persisted_plans", device_id)
            return None
        cloud_plans = sum(int(plan["planId"]) > 0 for plan in plans)
        local_plans = sum(int(plan["planId"]) < 0 for plan in plans)
        started_at = self._clock()
        _LOGGER.info(
            "SCHEDULE RESYNC TX device=%s plans_total=%d cloud=%d local=%d reason=device_online",
            device_id,
            len(plans),
            cloud_plans,
            local_plans,
        )
        self._submit(
            device_id,
            protocol.Command.FEEDING_PLAN_SERVICE,
            "schedule:resync",
            {"plans": plans},
            {"cmd": protocol.Command.FEEDING_PLAN_SERVICE, "ts": _timestamp_ms(), "plans": plans},
        )
        _LOGGER.info(
            "SCHEDULE RESYNC ACK device=%s latency_ms=%d",
            device_id,
            int((self._clock() - started_at) * MILLISECONDS_PER_SECOND),
        )
        return ScheduleResyncResult(device_id, len(plans), cloud_plans, local_plans)

    def observe_device_message(
        self, device_id: str, topic: str, payload: bytes
    ) -> LocalControlAckRoute:
        """Resolve one ACK and state whether its natural post may reach PETLIBRO."""
        if topic != f"{protocol.topic_prefix(device_id)}/device/service/post":
            return LocalControlAckRoute.NOT_MATCHED
        try:
            body = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return LocalControlAckRoute.NOT_MATCHED
        if not isinstance(body, dict):
            return LocalControlAckRoute.NOT_MATCHED
        message_id = body.get("msgId")
        command = body.get("cmd")
        if not isinstance(message_id, str) or not isinstance(command, str):
            return LocalControlAckRoute.NOT_MATCHED
        with self._lock:
            pending = self._pending.get((device_id, message_id))
            if pending is None or pending.command != command:
                return LocalControlAckRoute.NOT_MATCHED
            pending.code = body.get("code")
            pending.completed.set()
        route = (
            LocalControlAckRoute.LOCAL_ONLY
            if command == protocol.Command.FEEDING_PLAN_SERVICE
            else LocalControlAckRoute.FORWARD_TO_CLOUD
        )
        _LOGGER.info(
            "DEVICE CONTROL ACK device_id=%s msgId=%s cmd=%s code=%s route=%s",
            device_id,
            message_id,
            command,
            body.get("code"),
            route.name,
        )
        return route

    def _submit_schedule(self, device_id: str, plans: list[dict[str, Any]], control_name: str) -> dict[str, Any]:
        """Send the current schedule as a complete, feeder-confirmed snapshot."""
        response = self._submit(
            device_id,
            protocol.Command.FEEDING_PLAN_SERVICE,
            control_name,
            {"plans": plans},
            {"cmd": protocol.Command.FEEDING_PLAN_SERVICE, "ts": _timestamp_ms(), "plans": plans},
        )
        self._shadow.replace_local_schedule_plans(device_id, plans)
        return response

    def _submit(
        self, device_id: str, command: str, control_name: str, values: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Publish one local command and wait for the correlated feeder ACK."""
        context = self._devices.get_by_device_id(device_id)
        if context is None or context.product_id != SUPPORTED_PRODUCT_ID:
            raise ControlStateUnavailableError("Device is not bridged or supported")
        if not self._presence.is_online(device_id):
            self._increment("control_rejected")
            raise ControlOfflineError("Device is offline")
        message_id = uuid.uuid4().hex
        pending = _PendingControl(command, control_name, values, self._clock(), threading.Event())
        with self._lock:
            if device_id in self._active_devices:
                self._increment_locked("control_rejected")
                raise ControlBusyError(f"A control request is already pending for device {device_id}")
            self._active_devices.add(device_id)
            self._pending[(device_id, message_id)] = pending
            self._increment_locked("control_requests")
        payload["msgId"] = message_id
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        _LOGGER.info("CONTROL %s requested device_id=%s msgId=%s cmd=%s", control_name, device_id, message_id, command)
        try:
            if not self._publish_local_control(device_id, context.product_id, raw):
                self._increment("control_rejected")
                raise ControlPublishError("Local MQTT publication failed")
            _LOGGER.info(
                "LOCAL CONTROL TX device_id=%s msgId=%s cmd=%s control=%s",
                device_id,
                message_id,
                command,
                control_name,
            )
            if not pending.completed.wait(self._ack_timeout_seconds):
                self._increment("control_timeout")
                raise ControlAckTimeoutError("Device acknowledgement timeout")
            if type(pending.code) is not int or pending.code != 0:
                self._increment("control_rejected")
                raise ControlAckRejectedError(control_name, pending.code)
            self._increment("control_success")
            _LOGGER.info("CONTROL %s confirmed device_id=%s msgId=%s latency_ms=%d", control_name, device_id, message_id, int((self._clock() - pending.sent_at) * 1000))
            capability = CONTROL_CAPABILITIES.get(control_name)
            return {"success": True, "device_id": device_id, "control": control_name, "value": values, "device_ack": True, "cloud_sync_behavior": "confirmed" if capability and capability.cloud_sync_confirmed else "unknown"}
        finally:
            with self._lock:
                self._pending.pop((device_id, message_id), None)
                self._active_devices.discard(device_id)

    def _effective_settings(self, device_id: str) -> dict[str, Any]:
        """Use acknowledged local values over cloud desired values in the UI/builders."""
        return {**self._shadow.get_desired(device_id), **self._shadow.get_local_confirmed(device_id)}

    def _schedule_plans(self, device_id: str) -> list[dict[str, Any]]:
        """Return local overrides plus last cloud plans in one deterministic snapshot."""
        stored = self._shadow.get_schedule_plans(device_id)
        if stored:
            return [dict(entry.plan) for entry in stored]
        fallback = self._shadow.get_feeding_plans(device_id)
        return [dict(plan) for plan in fallback.plans] if fallback else []

    def _persisted_schedule_plans(self, device_id: str) -> list[dict[str, Any]]:
        """Return the known local and cloud schedule rows for resynchronization."""
        return [
            dict(entry.plan)
            for entry in self._shadow.get_schedule_plans(device_id)
            if isinstance(entry.plan.get("planId"), int) and int(entry.plan["planId"]) != 0
        ]

    def _increment(self, counter: str) -> None:
        with self._lock:
            self._increment_locked(counter)

    def _increment_locked(self, counter: str) -> None:
        self._counters[counter] = self._counters.get(counter, 0) + 1


# Kept as a public alias to preserve the existing constructor/import seam.
SoundSwitchController = DeviceControlController


def _timestamp_ms() -> int:
    """Return a real Unix millisecond timestamp for MQTT `ts`."""
    return int(time.time() * MILLISECONDS_PER_SECOND)


def _sync_time_ms() -> int:
    """Return PETLIBRO's second-truncated schedule syncTime format."""
    return int(time.time()) * MILLISECONDS_PER_SECOND


def _populate_time_fields(values: dict[str, Any], timezone: ZoneInfo) -> None:
    """Add UTC counterparts for known local HH:MM fields using real DST rules."""
    pairs = (
        ("motionDetectionStartTime", "motionDetectionStartTimeUtc"),
        ("motionDetectionEndTime", "motionDetectionEndTimeUtc"),
        ("soundDetectionStartTime", "soundDetectionStartTimeUtc"),
        ("soundDetectionEndTime", "soundDetectionEndTimeUtc"),
        ("soundStartTime", "soundStartTimeUtc"),
        ("soundEndTime", "soundEndTimeUtc"),
        ("lightingStartTime", "lightingStartTimeUtc"),
        ("lightingEndTime", "lightingEndTimeUtc"),
        ("cameraStartTime", "cameraStartTimeUtc"),
        ("cameraEndTime", "cameraEndTimeUtc"),
        ("videoRecordStartTime", "videoRecordStartTimeUtc"),
        ("videoRecordEndTime", "videoRecordEndTimeUtc"),
    )
    today = datetime.now(timezone).date()
    for local_key, utc_key in pairs:
        local_time = values.get(local_key)
        if not isinstance(local_time, str):
            continue
        parsed = _parse_time(local_time)
        values[utc_key] = datetime.combine(today, parsed, timezone).astimezone(ZoneInfo("UTC")).strftime("%H:%M")


def _populate_duration_fields(values: dict[str, Any]) -> None:
    """Mirror PETLIBRO's direct minute difference, including negative values."""
    for start_key, end_key, duration_key in (
        ("soundStartTime", "soundEndTime", "soundTimes"),
        ("lightingStartTime", "lightingEndTime", "lightingTimes"),
    ):
        start = values.get(start_key)
        end = values.get(end_key)
        if isinstance(start, str) and isinstance(end, str):
            values[duration_key] = _minutes(end) - _minutes(start)


def _parse_time(value: str) -> LocalTime:
    """Validate an HH:MM string and return a datetime.time instance."""
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as error:
        raise ControlStateUnavailableError("Time must use HH:MM") from error


def _minutes(value: str) -> int:
    """Convert a validated local time into its minute-of-day index."""
    parsed = _parse_time(value)
    return parsed.hour * 60 + parsed.minute


def _next_local_plan_id(plans: list[dict[str, Any]]) -> int:
    """Return a new per-device negative plan ID without reuse."""
    negatives = [int(plan["planId"]) for plan in plans if isinstance(plan.get("planId"), int) and int(plan["planId"]) < 0]
    return min(negatives, default=0) - 1


def _validate_plan(plan: dict[str, Any]) -> None:
    """Validate the strict, non-dangerous feeder schedule shape."""
    if not isinstance(plan.get("planId"), int):
        raise ControlStateUnavailableError("planId must be an integer")
    if not isinstance(plan.get("executionTime"), str):
        raise ControlStateUnavailableError("executionTime must use HH:MM")
    _parse_time(plan["executionTime"])
    grain_num = plan.get("grainNum")
    audio_times = plan.get("audioTimes")
    repeat_day = plan.get("repeatDay")
    if type(grain_num) is not int or not MIN_GRAIN_NUM <= grain_num <= MAX_GRAIN_NUM:
        raise ControlStateUnavailableError("grainNum must be between 1 and 48")
    if type(audio_times) is not int or not MIN_AUDIO_TIMES <= audio_times <= MAX_AUDIO_TIMES:
        raise ControlStateUnavailableError("audioTimes must be between 1 and 5")
    if type(plan.get("enableAudio")) is not bool:
        raise ControlStateUnavailableError("enableAudio must be a boolean")
    if not isinstance(repeat_day, list) or any(type(day) is not int or day not in range(0, 8) for day in repeat_day):
        raise ControlStateUnavailableError("repeatDay must contain only 0..7")
    plan["repeatDay"] = sorted({day for day in repeat_day if day != 0})


def _reject_time_collision(plans: list[dict[str, Any]], candidate: dict[str, Any], ignored_plan_id: int | None) -> None:
    """Reject duplicate active execution times, mirroring the official app."""
    for plan in plans:
        if plan.get("planId") == ignored_plan_id or not plan.get("repeatDay"):
            continue
        if plan.get("executionTime") == candidate["executionTime"] and candidate.get("repeatDay"):
            raise ControlStateUnavailableError("Another active plan already uses this time")
