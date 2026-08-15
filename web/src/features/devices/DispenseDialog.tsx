import { createPortal } from "react-dom";
import { useRef, useState, type JSX, type RefObject } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { dispense } from "../../api/devices";
import { queryKeys } from "../../api/queryKeys";
import { useModalDialog } from "../../components/modalDialog";

const MAX_PORTIONS = 48;
const MIN_PORTIONS = 1;

interface DispenseDialogProps {
  deviceId: string;
  onClose: () => void;
  triggerRef: RefObject<HTMLButtonElement | null>;
}

/** Show one focus-managed, ACK-backed manual dispense confirmation dialog. */
export function DispenseDialog({ deviceId, onClose, triggerRef }: DispenseDialogProps): JSX.Element {
  const dialogRef = useRef<HTMLElement>(null);
  const mutationPendingRef = useRef(false);
  const [portions, setPortions] = useState(MIN_PORTIONS);
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => dispense(deviceId, portions),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.home });
      onClose();
    },
  });
  mutationPendingRef.current = mutation.isPending;
  const adjust = (delta: number): void => setPortions((value) => Math.min(MAX_PORTIONS, Math.max(MIN_PORTIONS, value + delta)));
  useModalDialog({ closeDisabled: mutationPendingRef.current, dialogRef, onClose, triggerRef });
  return createPortal(<div className="dialog-backdrop" role="presentation"><section aria-describedby="dispense-description" aria-labelledby="dispense-title" aria-modal="true" className="dialog" ref={dialogRef} role="dialog"><header className="dialog__heading"><h2 id="dispense-title">Dispense now</h2><button aria-label="Close dispense dialog" disabled={mutation.isPending} onClick={onClose} type="button">×</button></header><p id="dispense-description">This action runs only after the feeder confirms it.</p><div className="quantity-control"><button aria-label="Decrease portions" disabled={mutation.isPending || portions === MIN_PORTIONS} onClick={() => adjust(-1)} type="button">−</button><output aria-label="Portions to dispense" aria-live="polite">{portions}</output><button aria-label="Increase portions" disabled={mutation.isPending || portions === MAX_PORTIONS} onClick={() => adjust(1)} type="button">+</button></div>{mutation.isError && <p className="form-error" role="alert">{mutation.error.message}</p>}<footer><button disabled={mutation.isPending} onClick={onClose} type="button">Cancel</button><button className="primary-button" disabled={mutation.isPending} onClick={() => mutation.mutate()} type="button">{mutation.isPending ? "Dispensing…" : "Dispense"}</button></footer></section></div>, document.body);
}
