import type { AdvancedDeviceDetail, CameraAvailability, DailyDeviceDetail, HomeResponse } from "../types/api";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function invalidProjection(name: string): never {
  throw new Error(`Unexpected ${name} response from the relay.`);
}

function hasCameraShape(value: unknown): value is CameraAvailability {
  return isRecord(value)
    && typeof value.available === "boolean"
    && typeof value.bridge_registered === "boolean"
    && typeof value.go2rtc_reachable === "boolean"
    && typeof value.online === "boolean"
    && (value.media_consumers === undefined || typeof value.media_consumers === "number");
}

function hasDailyDeviceShape(value: unknown): boolean {
  return isRecord(value)
    && typeof value.device_id === "string"
    && (typeof value.product_id === "string" || value.product_id === null)
    && typeof value.local_state === "string"
    && (typeof value.last_seen_at === "number" || value.last_seen_at === null)
    && (typeof value.rssi === "number" || value.rssi === null)
    && Array.isArray(value.schedule)
    && value.schedule.every(hasDailyScheduleShape);
}

function hasSettingEntryShape(value: unknown): boolean {
  return isRecord(value)
    && typeof value.key === "string"
    && (typeof value.value === "boolean" || typeof value.value === "number" || typeof value.value === "string");
}

function hasSchedulePlanShape(value: unknown): boolean {
  return isRecord(value) && "plan" in value && "source" in value && "updated_at" in value;
}

function hasDailyScheduleShape(value: unknown): boolean {
  return isRecord(value)
    && typeof value.execution_time === "string"
    && typeof value.grain_num === "number"
    && Array.isArray(value.repeat_day)
    && value.repeat_day.every((day) => typeof day === "number");
}

function hasActivityShape(value: unknown): boolean {
  return isRecord(value)
    && typeof value.kind === "string"
    && (value.timestamp === null || typeof value.timestamp === "number");
}

function hasControlCapabilityShape(value: unknown): boolean {
  return isRecord(value)
    && typeof value.writable === "boolean"
    && typeof value.device_online === "boolean"
    && typeof value.required_state_available === "boolean"
    && typeof value.pending === "boolean";
}

function hasControlsShape(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return Object.entries(value).every(([key, capability]) => key === "counters"
    ? isRecord(capability) && Object.values(capability).every((count) => typeof count === "number")
    : hasControlCapabilityShape(capability));
}

function hasAdvancedLogShape(value: unknown): boolean {
  return isRecord(value)
    && typeof value.component === "string"
    && typeof value.level === "string"
    && typeof value.message === "string"
    && (value.timestamp === null || typeof value.timestamp === "number" || typeof value.timestamp === "string");
}

/** Validate the bounded Home projection before a component can render it. */
export function parseHomeResponse(value: unknown): HomeResponse {
  if (!isRecord(value) || !isRecord(value.status) || !Array.isArray(value.devices)) invalidProjection("home");
  if (!value.devices.every((device) => hasDailyDeviceShape(device) && hasCameraShape(device.camera))) invalidProjection("home device");
  return value as unknown as HomeResponse;
}

/** Validate the actual /daily response shape, including root-level camera state. */
export function parseDailyDeviceDetail(value: unknown): DailyDeviceDetail {
  if (
    !isRecord(value)
    || !hasDailyDeviceShape(value.device)
    || !hasCameraShape(value.camera)
    || !isRecord(value.state)
    || !Array.isArray(value.state.desired)
    || !value.state.desired.every(hasSettingEntryShape)
    || !Array.isArray(value.state.local_confirmed)
    || !value.state.local_confirmed.every(hasSettingEntryShape)
    || !Array.isArray(value.state.schedule_plans)
    || !value.state.schedule_plans.every(hasSchedulePlanShape)
    || !hasControlsShape(value.controls)
    || !Array.isArray(value.activity)
    || !value.activity.every(hasActivityShape)
  ) {
    invalidProjection("daily device detail");
  }
  return value as unknown as DailyDeviceDetail;
}

/** Validate the small camera availability projection consumed by CameraPage. */
export function parseCameraAvailability(value: unknown): CameraAvailability {
  if (!hasCameraShape(value)) invalidProjection("camera");
  return value;
}

/** Validate Advanced enough to render a safe fallback instead of throwing in a view. */
export function parseAdvancedDeviceDetail(value: unknown): AdvancedDeviceDetail {
  if (!isRecord(value) || !isRecord(value.device) || !isRecord(value.connectivity) || !isRecord(value.camera) || !isRecord(value.relay) || !isRecord(value.state_summary) || !Array.isArray(value.logs) || !value.logs.every(hasAdvancedLogShape)) {
    invalidProjection("advanced device detail");
  }
  return value as unknown as AdvancedDeviceDetail;
}
