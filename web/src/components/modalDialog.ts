import { useEffect, type RefObject } from "react";

export function focusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>("button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"));
}

interface ModalDialogOptions {
  closeDisabled?: boolean;
  dialogRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  triggerRef: RefObject<HTMLElement | null>;
}

/** Trap focus and isolate app content while a portalled modal dialog is open. */
export function useModalDialog({ closeDisabled = false, dialogRef, onClose, triggerRef }: ModalDialogOptions): void {
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog === null) return undefined;
    const backgroundElements = Array.from(document.body.children)
      .filter((element) => !element.contains(dialog))
      .map((element) => ({ element, inert: element.hasAttribute("inert"), ariaHidden: element.getAttribute("aria-hidden") }));
    for (const background of backgroundElements) {
      background.element.setAttribute("inert", "");
      background.element.setAttribute("aria-hidden", "true");
    }
    focusableElements(dialog)[0]?.focus();
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
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      for (const background of backgroundElements) {
        if (background.inert) background.element.setAttribute("inert", ""); else background.element.removeAttribute("inert");
        if (background.ariaHidden === null) background.element.removeAttribute("aria-hidden"); else background.element.setAttribute("aria-hidden", background.ariaHidden);
      }
      triggerRef.current?.focus();
    };
  }, [closeDisabled, dialogRef, onClose, triggerRef]);
}
