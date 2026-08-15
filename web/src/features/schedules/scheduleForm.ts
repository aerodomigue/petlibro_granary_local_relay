import type { Schedule, ScheduleCreateRequest, ScheduleDay, ScheduleFormValues } from "../../types/api";

export const SCHEDULE_DAYS: ReadonlyArray<{ label: string; value: ScheduleDay }> = [
  { label: "Mon", value: 1 }, { label: "Tue", value: 2 }, { label: "Wed", value: 3 },
  { label: "Thu", value: 4 }, { label: "Fri", value: 5 }, { label: "Sat", value: 6 }, { label: "Sun", value: 7 },
];

const EVERY_DAY: ScheduleDay[] = SCHEDULE_DAYS.map(({ value }) => value);

/** Convert a persisted plan into a form-owned draft. */
export function scheduleToFormValues(plan?: Schedule): ScheduleFormValues {
  const repeatDay = plan?.repeatDay ?? EVERY_DAY;
  return {
    executionTime: plan?.executionTime ?? "07:30",
    grainNum: plan?.grainNum ?? 1,
    enableAudio: plan?.enableAudio ?? false,
    audioTimes: plan?.audioTimes ?? 1,
    repeatDay,
    repeatMode: repeatDay.length === 0 ? "never" : repeatDay.length === EVERY_DAY.length ? "every" : "custom",
  };
}

/** Build the only schedule payload accepted by the typed relay API. */
export function scheduleFormRequest(values: ScheduleFormValues): ScheduleCreateRequest {
  return {
    executionTime: values.executionTime,
    grainNum: values.grainNum,
    enableAudio: values.enableAudio,
    audioTimes: values.audioTimes,
    repeatDay: values.repeatMode === "every" ? EVERY_DAY : values.repeatMode === "never" ? [] : values.repeatDay,
  };
}

/** Format one plan's active days without exposing backend representation. */
export function scheduleDaysLabel(days: ScheduleDay[]): string {
  if (days.length === 0) return "Disabled";
  if (days.length === EVERY_DAY.length) return "Every day";
  return SCHEDULE_DAYS.filter(({ value }) => days.includes(value)).map(({ label }) => label).join(" · ");
}
