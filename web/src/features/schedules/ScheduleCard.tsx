import { useRef, type JSX } from "react";

import type { ScheduleSnapshot } from "../../types/api";
import { scheduleDaysLabel } from "./scheduleForm";

interface ScheduleCardProps {
  entry: ScheduleSnapshot;
  onDelete: (entry: ScheduleSnapshot, trigger: HTMLButtonElement) => void;
  onEdit: (entry: ScheduleSnapshot, trigger: HTMLButtonElement) => void;
  onToggle: (entry: ScheduleSnapshot, trigger: HTMLButtonElement) => void;
  disabled: boolean;
}

/** Render one schedule as a readable, mobile-friendly card. */
export function ScheduleCard({ disabled, entry, onDelete, onEdit, onToggle }: ScheduleCardProps): JSX.Element {
  const editRef = useRef<HTMLButtonElement>(null);
  const deleteRef = useRef<HTMLButtonElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const active = entry.plan.repeatDay.length > 0;
  return <article className="schedule-card">
    <div className="schedule-card__summary"><time>{entry.plan.executionTime}</time><strong>{entry.plan.grainNum} {entry.plan.grainNum === 1 ? "portion" : "portions"}</strong><p><span aria-label={active ? "Active" : "Disabled"} className={`schedule-status schedule-status--${active ? "active" : "disabled"}`}>{active ? "✓ Active" : "○ Disabled"}</span> · {scheduleDaysLabel(entry.plan.repeatDay)}{entry.plan.enableAudio ? ` · Sound: ${entry.plan.audioTimes} ${entry.plan.audioTimes === 1 ? "time" : "times"}` : ""}</p></div>
    <div className="schedule-card__actions"><button aria-label={`Edit scheduled meal at ${entry.plan.executionTime}`} disabled={disabled} onClick={() => onEdit(entry, editRef.current!)} ref={editRef} type="button">Edit</button><button aria-label={`${active ? "Disable" : "Enable"} scheduled meal at ${entry.plan.executionTime}`} disabled={disabled} onClick={() => onToggle(entry, toggleRef.current!)} ref={toggleRef} type="button">{active ? "Disable" : "Enable"}</button><button aria-label={`Delete scheduled meal at ${entry.plan.executionTime}`} className="danger-button" disabled={disabled} onClick={() => onDelete(entry, deleteRef.current!)} ref={deleteRef} type="button">Delete</button></div>
  </article>;
}
