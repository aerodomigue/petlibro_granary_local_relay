import type { JSX } from "react";

import type { DailySchedulePlan } from "../types/api";
import { todaySchedules } from "../features/devices/presentation";

interface TodayScheduleProps {
  schedule: readonly DailySchedulePlan[];
}

/** Show today's configured meals without inferring that a feeding completed. */
export function TodaySchedule({ schedule }: TodayScheduleProps): JSX.Element {
  const plannedMeals = todaySchedules(schedule);
  return <section className="schedule-summary">
    <h3>Today’s schedule</h3>
    {plannedMeals.length === 0
      ? <p>No meal planned today.</p>
      : <ul>{plannedMeals.map((plan) => <li key={`${plan.execution_time}-${plan.grain_num}`}>○ {plan.execution_time} · {plan.grain_num} portions</li>)}</ul>}
  </section>;
}
