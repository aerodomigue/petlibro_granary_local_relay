import type { ControlGroup } from "../../api/deviceDetails";
import type { SettingValue } from "../../types/api";

export type SettingField =
  | { key: string; label: string; type: "boolean" }
  | { key: string; label: string; type: "number"; max: number; min: number }
  | { key: string; label: string; type: "select"; options: ReadonlyArray<readonly [string, string]> }
  | { key: string; label: string; type: "time" };

export interface DeviceSettingGroup {
  fields: ReadonlyArray<SettingField>;
  primary: string;
  route: ControlGroup;
  title: string;
}

const scheduleOptions = [["1", "All day"], ["2", "Custom hours"]] as const;

/** Mirrors the existing allowlisted backend groups; no UI field can select arbitrary MQTT data. */
export const DEVICE_SETTING_GROUPS: ReadonlyArray<DeviceSettingGroup> = [
  { route: "motion", primary: "motionDetectionSwitch", title: "Motion detection", fields: [
    { key: "motionDetectionSwitch", label: "Enable motion detection", type: "boolean" }, { key: "motionDetectionAgingType", label: "Schedule", type: "select", options: scheduleOptions }, { key: "motionDetectionStartTime", label: "Start time", type: "time" }, { key: "motionDetectionEndTime", label: "End time", type: "time" }, { key: "motionDetectionSensitivity", label: "Sensitivity", type: "select", options: [["LOW", "Low"], ["MEDIUM", "Medium"], ["HIGH", "High"]] }, { key: "motionDetectionRange", label: "Detection range", type: "select", options: [["SMALL", "Small"], ["MEDIUM", "Medium"], ["LARGE", "Large"]] },
  ] },
  { route: "sound-detection", primary: "soundDetectionSwitch", title: "Sound detection", fields: [
    { key: "soundDetectionSwitch", label: "Enable sound detection", type: "boolean" }, { key: "soundDetectionAgingType", label: "Schedule", type: "select", options: scheduleOptions }, { key: "soundDetectionStartTime", label: "Start time", type: "time" }, { key: "soundDetectionEndTime", label: "End time", type: "time" }, { key: "soundDetectionSensitivity", label: "Sensitivity", type: "select", options: [["LOW", "Low"], ["MEDIUM", "Medium"], ["HIGH", "High"]] },
  ] },
  { route: "sound", primary: "soundSwitch", title: "Speaker", fields: [
    { key: "soundSwitch", label: "Enable device sound", type: "boolean" }, { key: "volume", label: "Volume", type: "number", min: 0, max: 100 }, { key: "soundAgingType", label: "Schedule", type: "select", options: scheduleOptions }, { key: "soundStartTime", label: "Start time", type: "time" }, { key: "soundEndTime", label: "End time", type: "time" },
  ] },
  { route: "light", primary: "lightSwitch", title: "Lighting", fields: [
    { key: "lightSwitch", label: "Enable light", type: "boolean" }, { key: "filterLedSwitch", label: "Filter LED", type: "boolean" }, { key: "lightAgingType", label: "Schedule", type: "select", options: scheduleOptions }, { key: "lightingStartTime", label: "Start time", type: "time" }, { key: "lightingEndTime", label: "End time", type: "time" },
  ] },
  { route: "camera", primary: "cameraSwitch", title: "Camera", fields: [
    { key: "cameraSwitch", label: "Enable camera", type: "boolean" }, { key: "cameraAgingType", label: "Schedule", type: "select", options: scheduleOptions }, { key: "cameraStartTime", label: "Start time", type: "time" }, { key: "cameraEndTime", label: "End time", type: "time" }, { key: "resolution", label: "Resolution", type: "select", options: [["P720", "720p"], ["P1080", "1080p"]] }, { key: "nightVision", label: "Night vision", type: "select", options: [["AUTOMATIC", "Automatic"], ["OPEN", "Always on"], ["CLOSE", "Off"]] },
  ] },
  { route: "video", primary: "videoRecordSwitch", title: "Local recording", fields: [
    { key: "videoRecordSwitch", label: "Enable local recording", type: "boolean" }, { key: "videoRecordMode", label: "Mode", type: "select", options: [["CONTINUOUS", "Continuous"], ["MOTION_DETECTION", "Motion detection"]] }, { key: "videoRecordAgingType", label: "Schedule", type: "select", options: scheduleOptions }, { key: "videoRecordStartTime", label: "Start time", type: "time" }, { key: "videoRecordEndTime", label: "End time", type: "time" }, { key: "videoWatermarkSwitch", label: "Watermark", type: "boolean" },
  ] },
  { route: "feeding-video", primary: "feedingVideoSwitch", title: "Feeding video", fields: [
    { key: "feedingVideoSwitch", label: "Enable feeding video", type: "boolean" }, { key: "enableVideoStartFeedingPlan", label: "Record before scheduled meals", type: "boolean" }, { key: "beforeFeedingPlanTime", label: "Minutes before", type: "number", min: 1, max: 5 }, { key: "automaticRecording", label: "Automatic recording length", type: "number", min: 1, max: 5 }, { key: "enableVideoAfterManualFeeding", label: "Record after manual dispense", type: "boolean" }, { key: "afterManualFeedingTime", label: "Minutes after", type: "number", min: 1, max: 5 },
  ] },
  { route: "bowl", primary: "bowlMode", title: "Bowl configuration", fields: [{ key: "bowlMode", label: "Bowl mode", type: "select", options: [["SINGLE_BOWL", "Single bowl"], ["DOUBLE_BOWL", "Double bowl"]] }] },
];

export type DeviceSettingFormValues = Record<string, SettingValue | undefined>;
