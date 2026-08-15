import { useState, type JSX, type RefObject } from "react";

import type { Schedule } from "../../types/api";
import { ScheduleDialog } from "./ScheduleDialog";

interface ScheduleDeleteDialogProps {
  onClose: () => void;
  onDelete: () => Promise<void>;
  pending: boolean;
  plan: Schedule;
  submitDisabled?: boolean;
  triggerRef: RefObject<HTMLElement | null>;
}

/** Confirm one destructive, feeder-confirmed plan deletion. */
export function ScheduleDeleteDialog({ onClose, onDelete, pending, plan, submitDisabled = false, triggerRef }: ScheduleDeleteDialogProps): JSX.Element {
  const [error, setError] = useState<string | null>(null);
  const deletePlan = (): void => {
    setError(null);
    void onDelete().catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Could not delete scheduled meal."));
  };
  return <ScheduleDialog closeDisabled={pending} describedBy="delete-schedule-description" labelledBy="delete-schedule-title" onClose={onClose} triggerRef={triggerRef}>
    <header className="dialog__heading"><h2 id="delete-schedule-title">Delete scheduled meal?</h2><button aria-label="Close delete schedule dialog" disabled={pending} onClick={onClose} type="button">×</button></header>
    <p id="delete-schedule-description">This removes the {plan.executionTime} meal from this feeder after its confirmation.</p>{error && <p className="form-error" role="alert">{error}</p>}
    <footer><button disabled={pending} onClick={onClose} type="button">Cancel</button><button className="danger-button" disabled={pending || submitDisabled} onClick={deletePlan} type="button">{pending ? "Deleting…" : "Delete"}</button></footer>
  </ScheduleDialog>;
}
