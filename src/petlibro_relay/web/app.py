"""FastAPI diagnostics plus explicitly confirmed feeder controls.

All routes are read-only except narrow, typed control endpoints. They
cannot choose MQTT topics, commands, fields, or payloads; those are built and
validated exclusively by the control service after device-local ACK.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..sound_switch_control import (
    ControlAckRejectedError,
    ControlAckTimeoutError,
    ControlBusyError,
    ControlOfflineError,
    ControlPublishError,
    ControlStateUnavailableError,
)
from .context import DashboardContext

MAX_CAMERA_WEBRTC_OFFER_BYTES = 256_000
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WEBRTC_SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
FRONTEND_DIST_DIRECTORY = Path(__file__).with_name("dist")
LEGACY_GLOBAL_ROUTE_ALIASES = frozenset({"cloud", "devices", "logs", "ntp", "queues", "state", "system"})


class ControlRequest(BaseModel):
    """The only accepted write body: one explicit boolean control state."""

    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool


class _StrictRequest(BaseModel):
    """Base request that rejects unknown fields instead of accepting MQTT-like blobs."""

    model_config = ConfigDict(extra="forbid", strict=True)


class MotionRequest(_StrictRequest):
    """Strict motion-detection group; `enabled` preserves the original route body."""

    enabled: bool | None = None
    motionDetectionSwitch: bool | None = None
    motionDetectionAgingType: Literal[1, 2] | None = None
    motionDetectionStartTime: str | None = None
    motionDetectionEndTime: str | None = None
    motionDetectionSensitivity: Literal["LOW", "MEDIUM", "HIGH"] | None = None
    motionDetectionRange: Literal["SMALL", "MEDIUM", "LARGE"] | None = None

    @model_validator(mode="after")
    def require_update(self) -> "MotionRequest":
        """Reject empty group writes."""
        if not self.model_dump(exclude_none=True):
            raise ValueError("At least one motion setting is required")
        return self


class SoundDetectionRequest(_StrictRequest):
    """Strict sound-detection settings."""

    soundDetectionSwitch: bool | None = None
    soundDetectionAgingType: Literal[1, 2] | None = None
    soundDetectionStartTime: str | None = None
    soundDetectionEndTime: str | None = None
    soundDetectionSensitivity: Literal["LOW", "MEDIUM", "HIGH"] | None = None


class SoundRequest(_StrictRequest):
    """Strict speaker settings, retaining the legacy `enabled` switch body."""

    enabled: bool | None = None
    soundSwitch: bool | None = None
    soundAgingType: Literal[1, 2] | None = None
    soundStartTime: str | None = None
    soundEndTime: str | None = None
    volume: int | None = Field(default=None, ge=0, le=100)


class LightRequest(_StrictRequest):
    """Strict feeder-light settings."""

    lightSwitch: bool | None = None
    lightAgingType: Literal[1, 2] | None = None
    lightingStartTime: str | None = None
    lightingEndTime: str | None = None
    filterLedSwitch: bool | None = None


class CameraRequest(_StrictRequest):
    """Strict camera availability and image settings."""

    cameraSwitch: bool | None = None
    cameraAgingType: Literal[1, 2] | None = None
    cameraStartTime: str | None = None
    cameraEndTime: str | None = None
    resolution: Literal["P720", "P1080"] | None = None
    nightVision: Literal["AUTOMATIC", "OPEN", "CLOSE"] | None = None


class VideoRequest(_StrictRequest):
    """Strict local SD recording settings."""

    videoRecordSwitch: bool | None = None
    videoRecordMode: Literal["CONTINUOUS", "MOTION_DETECTION"] | None = None
    videoRecordAgingType: Literal[1, 2] | None = None
    videoRecordStartTime: str | None = None
    videoRecordEndTime: str | None = None
    videoWatermarkSwitch: bool | None = None


class FeedingVideoRequest(_StrictRequest):
    """Strict feeding-video settings with validated minute ranges."""

    feedingVideoSwitch: bool | None = None
    enableVideoStartFeedingPlan: bool | None = None
    beforeFeedingPlanTime: int | None = Field(default=None, ge=1, le=5)
    automaticRecording: int | None = Field(default=None, ge=1, le=5)
    enableVideoAfterManualFeeding: bool | None = None
    afterManualFeedingTime: int | None = Field(default=None, ge=1, le=5)


class BowlRequest(_StrictRequest):
    """Strict physical bowl mode setting."""

    bowlMode: Literal["SINGLE_BOWL", "DOUBLE_BOWL"]


class DispenseRequest(_StrictRequest):
    """One immediate portion request, bounded to the feeder's supported range."""

    grainNum: int = Field(ge=1, le=48)


class ScheduleCreateRequest(_StrictRequest):
    """A safe feeder schedule plan with positive app-compatible limits."""

    executionTime: str
    grainNum: int = Field(ge=1, le=48)
    enableAudio: bool
    audioTimes: int = Field(ge=1, le=5)
    repeatDay: list[int]

    @field_validator("executionTime")
    @classmethod
    def validate_time(cls, value: str) -> str:
        """Keep route errors useful before the MQTT builder is reached."""
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Time must use HH:MM")
        return value

    @field_validator("repeatDay")
    @classmethod
    def validate_days(cls, value: list[int]) -> list[int]:
        """Allow only documented day values, including zero as ignored legacy padding."""
        if any(day not in range(0, 8) for day in value):
            raise ValueError("repeatDay must contain only 0..7")
        return value


class ScheduleUpdateRequest(_StrictRequest):
    """Partial schedule update; server keeps the known plan ID and snapshot."""

    executionTime: str | None = None
    grainNum: int | None = Field(default=None, ge=1, le=48)
    enableAudio: bool | None = None
    audioTimes: int | None = Field(default=None, ge=1, le=5)
    repeatDay: list[int] | None = None

    @field_validator("executionTime")
    @classmethod
    def validate_optional_time(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Time must use HH:MM")
        return value

    @field_validator("repeatDay")
    @classmethod
    def validate_optional_days(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and any(day not in range(0, 8) for day in value):
            raise ValueError("repeatDay must contain only 0..7")
        return value


def create_app(context: DashboardContext) -> FastAPI:
    """Create the React dashboard app with narrow typed feeder controls."""
    app = FastAPI(
        title="PETLIBRO Local Relay",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    bundle_available = FRONTEND_DIST_DIRECTORY.is_dir()
    if bundle_available:
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST_DIRECTORY / "assets"), name="assets")

    def dashboard_shell() -> FileResponse:
        """Return the single React shell without affecting API routes."""
        if not bundle_available:
            raise HTTPException(status_code=503, detail="React dashboard bundle is unavailable")
        return FileResponse(FRONTEND_DIST_DIRECTORY / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    @app.get("/settings", include_in_schema=False)
    def dashboard() -> FileResponse:
        """Serve the React dashboard for canonical global routes."""
        return dashboard_shell()

    @app.get("/devices", include_in_schema=False)
    @app.get("/cloud", include_in_schema=False)
    @app.get("/queues", include_in_schema=False)
    @app.get("/state", include_in_schema=False)
    @app.get("/ntp", include_in_schema=False)
    @app.get("/logs", include_in_schema=False)
    @app.get("/system", include_in_schema=False)
    def legacy_global_route() -> RedirectResponse:
        """Canonicalize former global dashboard routes to the React home view."""
        return RedirectResponse(url="/", status_code=308)

    @app.get("/devices/{device_id}", include_in_schema=False)
    def device_dashboard(device_id: str) -> FileResponse:
        """Serve a device-scoped dashboard only for a known safe device id."""
        if not DEVICE_ID_PATTERN.fullmatch(device_id) or context.device_detail(device_id, 1) is None:
            raise HTTPException(status_code=404, detail="Unknown device")
        return dashboard_shell()

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Report relay health; PETLIBRO cloud availability is informational only.

        A cloud outage - which is the situation this relay exists for - must
        never make the relay itself look unhealthy, so `healthy` reflects only
        what this process controls.
        """
        status = context.status()
        summary = status["devices"]
        payload = {
            "healthy": True,
            "relay": True,
            "local_mqtt": status["local_mqtt"]["connected"],
            "devices_known": summary["known"],
            "devices_local_online": summary["local_online"],
            "upstream_petlibro_online": summary["cloud_online"],
        }
        return JSONResponse(payload, status_code=200)

    @app.get("/api/home")
    def home() -> dict[str, object]:
        """Return the compact, non-diagnostic home-screen projection."""
        return context.home()

    @app.get("/api/devices/{device_id}/daily")
    def daily_device_detail(device_id: str) -> dict[str, object]:
        """Return normal user-facing data without raw state or credentials."""
        if not DEVICE_ID_PATTERN.fullmatch(device_id):
            raise HTTPException(status_code=404, detail="Unknown device")
        detail = context.daily_device_detail(device_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Unknown device")
        return detail

    @app.get("/api/devices/{device_id}/advanced")
    def advanced_device_detail(device_id: str) -> dict[str, object]:
        """Return bounded diagnostics for the opt-in Advanced UI."""
        if not DEVICE_ID_PATTERN.fullmatch(device_id):
            raise HTTPException(status_code=404, detail="Unknown device")
        detail = context.advanced_device_detail(device_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Unknown device")
        return detail

    @app.get("/api/devices/{device_id}/camera")
    def camera(device_id: str) -> dict[str, object]:
        """Return constrained go2rtc status without exposing source details."""
        if not DEVICE_ID_PATTERN.fullmatch(device_id) or context.device_detail(device_id, 1) is None:
            raise HTTPException(status_code=404, detail="Unknown device")
        return context.camera(device_id)

    @app.post("/api/devices/{device_id}/camera/webrtc", response_class=Response)
    async def camera_webrtc(device_id: str, request: Request) -> Response:
        """Proxy one device-scoped WHEP offer without exposing go2rtc to the UI."""
        if not DEVICE_ID_PATTERN.fullmatch(device_id) or context.device_detail(device_id, 1) is None:
            raise HTTPException(status_code=404, detail="Unknown device")
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type != "application/sdp":
            raise HTTPException(status_code=415, detail="WebRTC offer must use application/sdp")
        offer = await request.body()
        if not offer or len(offer) > MAX_CAMERA_WEBRTC_OFFER_BYTES:
            raise HTTPException(status_code=400, detail="Invalid WebRTC offer")
        viewer_id = request.headers.get("X-Relay-Viewer-ID", "")
        if not WEBRTC_SESSION_ID_PATTERN.fullmatch(viewer_id):
            raise HTTPException(status_code=409, detail="Camera viewer is unavailable")
        try:
            exchange = context.exchange_camera_webrtc(device_id, viewer_id, offer)
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail="Camera stream is unavailable") from error
        headers = {"X-Relay-WebRTC-Session": exchange.session_id} if exchange.session_id else None
        return Response(content=exchange.answer, status_code=201, media_type="application/sdp", headers=headers)

    @app.post("/api/devices/{device_id}/camera/viewers/{viewer_id}", status_code=204)
    def activate_camera_viewer(device_id: str, viewer_id: str) -> Response:
        """Activate one explicit logical camera viewer."""
        if (
            not DEVICE_ID_PATTERN.fullmatch(device_id)
            or context.device_detail(device_id, 1) is None
            or not WEBRTC_SESSION_ID_PATTERN.fullmatch(viewer_id)
        ):
            raise HTTPException(status_code=404, detail="Unknown camera viewer")
        if not context.activate_camera_viewer(device_id, viewer_id):
            raise HTTPException(status_code=503, detail="Camera stream is unavailable")
        return Response(status_code=204)

    @app.put("/api/devices/{device_id}/camera/viewers/{viewer_id}", status_code=204)
    def heartbeat_camera_viewer(device_id: str, viewer_id: str) -> Response:
        """Refresh an existing logical camera viewer."""
        if (
            not DEVICE_ID_PATTERN.fullmatch(device_id)
            or context.device_detail(device_id, 1) is None
            or not context.heartbeat_camera_viewer(device_id, viewer_id)
        ):
            raise HTTPException(status_code=404, detail="Unknown camera viewer")
        return Response(status_code=204)

    @app.delete("/api/devices/{device_id}/camera/viewers/{viewer_id}", status_code=204)
    def deactivate_camera_viewer(device_id: str, viewer_id: str) -> Response:
        """Deactivate one logical viewer."""
        if (
            not DEVICE_ID_PATTERN.fullmatch(device_id)
            or context.device_detail(device_id, 1) is None
            or not context.deactivate_camera_viewer(device_id, viewer_id, "client_closed")
        ):
            raise HTTPException(status_code=404, detail="Unknown camera viewer")
        return Response(status_code=204)

    @app.delete("/api/devices/{device_id}/camera/webrtc/{session_id}", status_code=204)
    def close_camera_webrtc(device_id: str, session_id: str) -> Response:
        """Release one opaque, device-scoped WHEP session after viewer teardown."""
        if not DEVICE_ID_PATTERN.fullmatch(device_id) or context.device_detail(device_id, 1) is None:
            raise HTTPException(status_code=404, detail="Unknown device")
        if not WEBRTC_SESSION_ID_PATTERN.fullmatch(session_id) or not context.close_camera_webrtc(device_id, session_id):
            raise HTTPException(status_code=404, detail="Unknown camera session")
        return Response(status_code=204)

    @app.patch("/api/devices/{device_id}/controls/sound")
    def set_device_sound(device_id: str, request: SoundRequest) -> dict[str, object]:
        """Set the device sound control validated for PETLIBRO cloud sync."""
        if not DEVICE_ID_PATTERN.fullmatch(device_id) or context.device_detail(device_id, 1) is None:
            raise HTTPException(status_code=404, detail="Unknown device")
        control = context.sound_switch_control
        if control is None:
            raise HTTPException(status_code=409, detail="Sound control is unavailable")
        values = request.model_dump(exclude_none=True)
        enabled = values.pop("enabled", None)
        return _run_control(
            control,
            device_id,
            lambda: control.set_sound_switch(device_id, enabled)
            if enabled is not None and not values
            else control.set_group(device_id, "sound", {**values, **({"soundSwitch": enabled} if enabled is not None else {})}),
        )

    @app.patch("/api/devices/{device_id}/controls/motion")
    def set_device_motion_detection(device_id: str, request: MotionRequest) -> dict[str, object]:
        """Set the independently confirmed local motion-detection control."""
        if not DEVICE_ID_PATTERN.fullmatch(device_id) or context.device_detail(device_id, 1) is None:
            raise HTTPException(status_code=404, detail="Unknown device")
        control = context.sound_switch_control
        if control is None:
            raise HTTPException(status_code=409, detail="Motion detection control is unavailable")
        values = request.model_dump(exclude_none=True)
        enabled = values.pop("enabled", None)
        return _run_control(
            control,
            device_id,
            lambda: control.set_motion_detection_switch(device_id, enabled)
            if enabled is not None and not values
            else control.set_group(device_id, "motion", {**values, **({"motionDetectionSwitch": enabled} if enabled is not None else {})}),
        )

    @app.patch("/api/devices/{device_id}/controls/sound-detection")
    def set_sound_detection(device_id: str, request: SoundDetectionRequest) -> dict[str, object]:
        return _set_group_endpoint(context, device_id, "sound_detection", request.model_dump(exclude_none=True))

    @app.patch("/api/devices/{device_id}/controls/light")
    def set_light(device_id: str, request: LightRequest) -> dict[str, object]:
        return _set_group_endpoint(context, device_id, "light", request.model_dump(exclude_none=True))

    @app.patch("/api/devices/{device_id}/controls/camera")
    def set_camera(device_id: str, request: CameraRequest) -> dict[str, object]:
        return _set_group_endpoint(context, device_id, "camera", request.model_dump(exclude_none=True))

    @app.patch("/api/devices/{device_id}/controls/video")
    def set_video(device_id: str, request: VideoRequest) -> dict[str, object]:
        return _set_group_endpoint(context, device_id, "video", request.model_dump(exclude_none=True))

    @app.patch("/api/devices/{device_id}/controls/feeding-video")
    def set_feeding_video(device_id: str, request: FeedingVideoRequest) -> dict[str, object]:
        return _set_group_endpoint(context, device_id, "feeding_video", request.model_dump(exclude_none=True))

    @app.patch("/api/devices/{device_id}/controls/bowl")
    def set_bowl(device_id: str, request: BowlRequest) -> dict[str, object]:
        return _set_group_endpoint(context, device_id, "bowl", request.model_dump())

    @app.post("/api/devices/{device_id}/dispense")
    def dispense(device_id: str, request: DispenseRequest) -> dict[str, object]:
        """Dispense now only after a local feeder acknowledgement."""
        control = _known_control(context, device_id)
        return _run_control(control, device_id, lambda: control.dispense(device_id, request.grainNum))

    @app.post("/api/devices/{device_id}/schedule")
    def create_schedule(device_id: str, request: ScheduleCreateRequest) -> dict[str, object]:
        return _schedule_endpoint(context, device_id, lambda control: control.create_schedule(device_id, request.model_dump()))

    @app.patch("/api/devices/{device_id}/schedule/{plan_id}")
    def update_schedule(device_id: str, plan_id: int, request: ScheduleUpdateRequest) -> dict[str, object]:
        values = request.model_dump(exclude_none=True)
        if not values:
            raise HTTPException(status_code=422, detail="At least one schedule field is required")
        return _schedule_endpoint(context, device_id, lambda control: control.update_schedule(device_id, plan_id, values))

    @app.delete("/api/devices/{device_id}/schedule/{plan_id}")
    def delete_schedule(device_id: str, plan_id: int) -> dict[str, object]:
        return _schedule_endpoint(context, device_id, lambda control: control.delete_schedule(device_id, plan_id))

    @app.get("/{frontend_path:path}", include_in_schema=False)
    def react_spa_fallback(frontend_path: str) -> Response:
        """Serve browser routes from the SPA while API misses remain HTTP 404."""
        if frontend_path == "api" or frontend_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Unknown API route")
        if frontend_path in LEGACY_GLOBAL_ROUTE_ALIASES:
            return RedirectResponse(url="/", status_code=308)
        return dashboard_shell()

    return app


def _known_control(context: DashboardContext, device_id: str) -> Any:
    """Return the explicit control service only for a known device."""
    if not DEVICE_ID_PATTERN.fullmatch(device_id) or context.device_detail(device_id, 1) is None:
        raise HTTPException(status_code=404, detail="Unknown device")
    control = context.sound_switch_control
    if control is None:
        raise HTTPException(status_code=409, detail="Local control is unavailable")
    return control


def _set_group_endpoint(context: DashboardContext, device_id: str, group: str, values: dict[str, Any]) -> dict[str, object]:
    """Run one typed group update without creating a generic MQTT endpoint."""
    if not values:
        raise HTTPException(status_code=422, detail="At least one control field is required")
    control = _known_control(context, device_id)
    return _run_control(control, device_id, lambda: control.set_group(device_id, group, values))


def _schedule_endpoint(
    context: DashboardContext, device_id: str, action: Callable[[Any], dict[str, object]]
) -> dict[str, object]:
    """Run one typed local schedule action with the normal ACK error mapping."""
    control = _known_control(context, device_id)
    return _run_control(control, device_id, lambda: action(control))


def _run_control(
    control: Any, device_id: str, action: Callable[[], dict[str, object]]
) -> dict[str, object]:
    """Map expected local-control failures to stable HTTP responses."""
    del control, device_id
    try:
        return action()
    except ControlOfflineError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ControlStateUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ControlBusyError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ControlPublishError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ControlAckTimeoutError as error:
        raise HTTPException(status_code=504, detail=str(error)) from error
    except ControlAckRejectedError as error:
        raise HTTPException(status_code=502, detail={"message": str(error), "device_ack": True, "code": error.code}) from error
