import { request } from "./client";
import type {
  DailyDeviceDetail,
  Schedule,
  ScheduleCreateRequest,
  ScheduleDay,
  ScheduleData,
  ScheduleMutationResult,
  ScheduleSnapshot,
  ScheduleUpdateRequest,
} from "../types/api";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isScheduleDay(value: unknown): value is ScheduleDay {
  return typeof value === "number" && Number.isInteger(value) && value >= 1 && value <= 7;
}

function isValidTime(value: unknown): value is string {
  return typeof value === "string" && /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value);
}

function isBoundedInteger(value: unknown, minimum: number, maximum: number): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= minimum && value <= maximum;
}

function isInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value);
}

/** Parse the safe daily schedule projection supplied by the relay. */
export function parseSchedules(detail: DailyDeviceDetail): ScheduleSnapshot[] {
  return detail.state.schedule_plans.flatMap((entry): ScheduleSnapshot[] => {
    if (!isRecord(entry.plan)) return [];
    const plan = entry.plan;
    const repeatDay = Array.isArray(plan.repeatDay) ? plan.repeatDay : null;
    if (
      !isInteger(plan.planId)
      || !isValidTime(plan.executionTime)
      || !isBoundedInteger(plan.grainNum, 1, 48)
      || typeof plan.enableAudio !== "boolean"
      || !isBoundedInteger(plan.audioTimes, 1, 5)
      || repeatDay === null
      || !repeatDay.every(isScheduleDay)
      || new Set(repeatDay).size !== repeatDay.length
      || typeof entry.source !== "string"
      || typeof entry.updated_at !== "number"
    ) return [];
    return [{
      plan: {
        planId: plan.planId,
        executionTime: plan.executionTime,
        grainNum: plan.grainNum,
        enableAudio: plan.enableAudio,
        audioTimes: plan.audioTimes,
        repeatDay,
        ...(typeof plan.syncTime === "number" ? { syncTime: plan.syncTime } : {}),
      },
      source: entry.source,
      updatedAt: entry.updated_at,
    }];
  });
}

/** Fetch the device-scoped, non-diagnostic schedule snapshot. */
export async function getSchedules(deviceId: string, signal?: AbortSignal): Promise<ScheduleData> {
  const detail = await request<DailyDeviceDetail>(`/api/devices/${encodeURIComponent(deviceId)}/daily`, { signal });
  return { device: detail.device, schedules: parseSchedules(detail) };
}

/** Create one feeder-confirmed schedule. */
export function createSchedule(deviceId: string, values: ScheduleCreateRequest): Promise<ScheduleMutationResult> {
  return request<ScheduleMutationResult>(`/api/devices/${encodeURIComponent(deviceId)}/schedule`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
}

/** Update one feeder-confirmed schedule without exposing a generic payload API. */
export function updateSchedule(deviceId: string, planId: number, values: ScheduleUpdateRequest): Promise<ScheduleMutationResult> {
  return request<ScheduleMutationResult>(`/api/devices/${encodeURIComponent(deviceId)}/schedule/${encodeURIComponent(planId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
}

/** Delete one plan after the feeder confirms the rebuilt snapshot. */
export function deleteSchedule(deviceId: string, planId: number): Promise<ScheduleMutationResult> {
  return request<ScheduleMutationResult>(`/api/devices/${encodeURIComponent(deviceId)}/schedule/${encodeURIComponent(planId)}`, { method: "DELETE" });
}
