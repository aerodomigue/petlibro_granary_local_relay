import { useState, type JSX } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { dispense } from "../../api/devices";
import { queryKeys } from "../../api/queryKeys";

const MIN_PORTIONS = 1;
const MAX_PORTIONS = 48;

interface DispenseDialogProps {
  deviceId: string;
  onClose: () => void;
}

export function DispenseDialog({ deviceId, onClose }: DispenseDialogProps): JSX.Element {
  const [portions, setPortions] = useState(MIN_PORTIONS);
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => dispense(deviceId, portions),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.home });
      onClose();
    },
  });
  const adjust = (delta: number): void => setPortions((value) => Math.min(MAX_PORTIONS, Math.max(MIN_PORTIONS, value + delta)));
  return <div className="dialog-backdrop" role="presentation"><section aria-labelledby="dispense-title" aria-modal="true" className="dialog" role="dialog"><h2 id="dispense-title">Dispense now</h2><p>This action runs only after the feeder confirms it.</p><div className="quantity-control"><button aria-label="Decrease portions" disabled={mutation.isPending || portions === MIN_PORTIONS} onClick={() => adjust(-1)} type="button">−</button><output aria-live="polite">{portions}</output><button aria-label="Increase portions" disabled={mutation.isPending || portions === MAX_PORTIONS} onClick={() => adjust(1)} type="button">+</button></div>{mutation.isError && <p className="form-error" role="alert">{mutation.error.message}</p>}<footer><button disabled={mutation.isPending} onClick={onClose} type="button">Cancel</button><button className="primary-button" disabled={mutation.isPending} onClick={() => mutation.mutate()} type="button">{mutation.isPending ? "Dispensing…" : "Dispense"}</button></footer></section></div>;
}
