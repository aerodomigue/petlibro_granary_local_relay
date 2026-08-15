import { useEffect, useRef, type JSX, type ReactNode, type RefObject } from "react";

interface ScheduleDialogProps {
  children: ReactNode;
  closeDisabled?: boolean;
  describedBy?: string;
  labelledBy: string;
  onClose: () => void;
  triggerRef: RefObject<HTMLElement | null>;
}

function focusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>("button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"));
}

/** Provide consistent focus containment for Schedule create, edit and delete dialogs. */
export function ScheduleDialog({ children, closeDisabled = false, describedBy, labelledBy, onClose, triggerRef }: ScheduleDialogProps): JSX.Element {
  const dialogRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog === null) return undefined;
    focusableElements(dialog)[0]?.focus();
    return () => { triggerRef.current?.focus(); };
  }, [triggerRef]);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog === null) return undefined;
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        if (closeDisabled) return;
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = focusableElements(dialog);
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => { document.removeEventListener("keydown", onKeyDown); };
  }, [closeDisabled, onClose]);
  return <div className="dialog-backdrop" role="presentation"><section aria-describedby={describedBy} aria-labelledby={labelledBy} aria-modal="true" className="dialog schedule-dialog" ref={dialogRef} role="dialog">{children}</section></div>;
}
