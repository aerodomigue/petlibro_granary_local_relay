import type { DailySchedulePlan } from "../../types/api";

/** Return only meals configured for the current local weekday. */
export function todaySchedules(schedule: readonly DailySchedulePlan[]): DailySchedulePlan[] {
  const mondayBasedDay = ((new Date().getDay() + 6) % 7) + 1;
  return schedule
    .filter((plan) => plan.repeat_day.includes(mondayBasedDay))
    .slice()
    .sort((first, second) => first.execution_time.localeCompare(second.execution_time));
}

/** Describe signal quality without exposing a raw RSSI value in daily views. */
export function wifiLabel(rssi: number | null): string {
  if (rssi === null) return "Wi-Fi unknown";
  if (rssi > -50) return "Wi-Fi excellent";
  if (rssi > -60) return "Wi-Fi good";
  if (rssi > -70) return "Wi-Fi fair";
  return "Wi-Fi weak";
}

/** Render a concise relative presence message for non-technical device views. */
export function lastSeenLabel(lastSeenAt: number | null): string {
  if (lastSeenAt === null) return "Last seen unavailable";
  const ageSeconds = Math.max(0, Math.round(Date.now() / 1_000 - lastSeenAt));
  if (ageSeconds < 60) return "Last seen just now";
  if (ageSeconds < 3_600) return `Last seen ${Math.floor(ageSeconds / 60)}m ago`;
  if (ageSeconds < 86_400) return `Last seen ${Math.floor(ageSeconds / 3_600)}h ago`;
  return `Last seen ${Math.floor(ageSeconds / 86_400)}d ago`;
}
