import type { JSX } from "react";

import type { ScheduleSnapshot } from "../../types/api";
import { ScheduleCard } from "./ScheduleCard";

interface ScheduleListProps {
  entries: ScheduleSnapshot[];
  onDelete: (entry: ScheduleSnapshot, trigger: HTMLButtonElement) => void;
  onEdit: (entry: ScheduleSnapshot, trigger: HTMLButtonElement) => void;
  onToggle: (entry: ScheduleSnapshot, trigger: HTMLButtonElement) => void;
  disabled: boolean;
}

/** List all known feeder schedules or a clear empty state. */
export function ScheduleList({ disabled, entries, onDelete, onEdit, onToggle }: ScheduleListProps): JSX.Element {
  if (entries.length === 0) return <section className="empty-state"><h2>No scheduled meals</h2><p>Add a meal to keep your feeder on a routine.</p></section>;
  return <div className="schedule-list">{entries.map((entry) => <ScheduleCard disabled={disabled} entry={entry} key={entry.plan.planId} onDelete={onDelete} onEdit={onEdit} onToggle={onToggle} />)}</div>;
}
