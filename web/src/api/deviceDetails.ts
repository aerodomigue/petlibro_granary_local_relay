import { request } from "./client";
import type { AdvancedDeviceDetail, DailyDeviceDetail, SettingValue } from "../types/api";

export function getDailyDevice(deviceId: string, signal?: AbortSignal): Promise<DailyDeviceDetail> {
  return request<DailyDeviceDetail>(`/api/devices/${encodeURIComponent(deviceId)}/daily`, { signal });
}

export function getAdvancedDevice(deviceId: string, signal?: AbortSignal): Promise<AdvancedDeviceDetail> {
  return request<AdvancedDeviceDetail>(`/api/devices/${encodeURIComponent(deviceId)}/advanced`, { signal });
}

export type ControlGroup = "motion" | "sound-detection" | "sound" | "light" | "camera" | "video" | "feeding-video" | "bowl";
type ControlFields<Keys extends string> = Partial<Readonly<Record<Keys, SettingValue>>>;

export type ControlUpdate =
  | ControlFields<"motionDetectionSwitch" | "motionDetectionAgingType" | "motionDetectionStartTime" | "motionDetectionEndTime" | "motionDetectionSensitivity" | "motionDetectionRange">
  | ControlFields<"soundDetectionSwitch" | "soundDetectionAgingType" | "soundDetectionStartTime" | "soundDetectionEndTime" | "soundDetectionSensitivity">
  | ControlFields<"soundSwitch" | "volume" | "soundAgingType" | "soundStartTime" | "soundEndTime">
  | ControlFields<"lightSwitch" | "filterLedSwitch" | "lightAgingType" | "lightingStartTime" | "lightingEndTime">
  | ControlFields<"cameraSwitch" | "cameraAgingType" | "cameraStartTime" | "cameraEndTime" | "resolution" | "nightVision">
  | ControlFields<"videoRecordSwitch" | "videoRecordMode" | "videoRecordAgingType" | "videoRecordStartTime" | "videoRecordEndTime" | "videoWatermarkSwitch">
  | ControlFields<"feedingVideoSwitch" | "enableVideoStartFeedingPlan" | "beforeFeedingPlanTime" | "automaticRecording" | "enableVideoAfterManualFeeding" | "afterManualFeedingTime">
  | ControlFields<"bowlMode">;

/** Send a group-specific typed backend request; the UI cannot choose an MQTT command or topic. */
export function updateControlGroup(deviceId: string, group: ControlGroup, values: ControlUpdate): Promise<void> {
  return request<void>(`/api/devices/${encodeURIComponent(deviceId)}/controls/${group}`, {
    body: JSON.stringify(values),
    headers: { "Content-Type": "application/json" },
    method: "PATCH",
  });
}
