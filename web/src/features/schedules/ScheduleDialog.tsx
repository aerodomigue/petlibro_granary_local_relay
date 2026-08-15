import { createPortal } from "react-dom";
import { useRef, type JSX, type ReactNode, type RefObject } from "react";

import { useModalDialog } from "../../components/modalDialog";

interface ScheduleDialogProps {
  children: ReactNode;
  closeDisabled?: boolean;
  describedBy?: string;
  labelledBy: string;
  onClose: () => void;
  triggerRef: RefObject<HTMLElement | null>;
}

/** Provide consistent focus containment for Schedule create, edit and delete dialogs. */
export function ScheduleDialog({ children, closeDisabled = false, describedBy, labelledBy, onClose, triggerRef }: ScheduleDialogProps): JSX.Element {
  const dialogRef = useRef<HTMLElement>(null);
  useModalDialog({ closeDisabled, dialogRef, onClose, triggerRef });
  return createPortal(<div className="dialog-backdrop" role="presentation"><section aria-describedby={describedBy} aria-labelledby={labelledBy} aria-modal="true" className="dialog schedule-dialog" ref={dialogRef} role="dialog">{children}</section></div>, document.body);
}
