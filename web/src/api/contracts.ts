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
    && typeof value.media_consumers === "number";
}

function hasDailyDeviceShape(value: unknown): boolean {
  return isRecord(value)
    && typeof value.device_id === "string"
    && (typeof value.product_id === "string" || value.product_id === null)
    && typeof value.local_state === "string"
    && (typeof value.last_seen_at === "number" || value.last_seen_at === null)
    && (typeof value.rssi === "number" || value.rssi === null)
    && Array.isArray(value.schedule);
}

function hasSettingEntryShape(value: unknown): boolean {
  return isRecord(value)
    && typeof value.key === "string"
    && (typeof value.value === "boolean" || typeof value.value === "number" || typeof value.value === "string");
}

function hasSchedulePlanShape(value: unknown): boolean {
  return isRecord(value) && "plan" in value && "source" in value && "updated_at" in value;
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
    || !isRecord(value.controls)
    || !Array.isArray(value.activity)
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
  if (!isRecord(value) || !isRecord(value.device) || !isRecord(value.connectivity) || !isRecord(value.camera) || !isRecord(value.relay) || !isRecord(value.state_summary) || !Array.isArray(value.logs)) {
    invalidProjection("advanced device detail");
  }
  return value as unknown as AdvancedDeviceDetail;
}
