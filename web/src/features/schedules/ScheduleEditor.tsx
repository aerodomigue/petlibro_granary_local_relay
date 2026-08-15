import { useState, type JSX, type RefObject } from "react";
import { useForm } from "react-hook-form";

import type { Schedule, ScheduleCreateRequest, ScheduleFormValues, ScheduleUpdateRequest } from "../../types/api";
import { ScheduleDialog } from "./ScheduleDialog";
import { SCHEDULE_DAYS, scheduleFormRequest, scheduleToFormValues } from "./scheduleForm";

interface ScheduleEditorProps {
  initialError?: string;
  initialValues?: ScheduleFormValues;
  onClose: () => void;
  onSave: (createValues: ScheduleCreateRequest, updateValues: ScheduleUpdateRequest) => Promise<void>;
  pending: boolean;
  plan?: Schedule;
  saveDisabled?: boolean;
  triggerRef: RefObject<HTMLElement | null>;
}

/** Edit a Schedule draft owned solely by React Hook Form until ACK-backed save succeeds. */
export function ScheduleEditor({ initialError, initialValues, onClose, onSave, pending, plan, saveDisabled = false, triggerRef }: ScheduleEditorProps): JSX.Element {
  const [submitError, setSubmitError] = useState<string | null>(initialError ?? null);
  const form = useForm<ScheduleFormValues>({ defaultValues: initialValues ?? scheduleToFormValues(plan) });
  const repeatMode = form.watch("repeatMode");
  const submit = form.handleSubmit(async (values) => {
    const request = scheduleFormRequest(values);
    if (request.repeatDay.length === 0 && values.repeatMode === "custom") {
      form.setError("repeatDay", { message: "Choose at least one day or select Disabled." });
      return;
    }
    setSubmitError(null);
    try {
      const dirty = form.formState.dirtyFields;
      const update: ScheduleUpdateRequest = {
        ...(dirty.executionTime ? { executionTime: request.executionTime } : {}),
        ...(dirty.grainNum ? { grainNum: request.grainNum } : {}),
        ...(dirty.enableAudio ? { enableAudio: request.enableAudio } : {}),
        ...(dirty.audioTimes ? { audioTimes: request.audioTimes } : {}),
        ...(dirty.repeatDay || dirty.repeatMode ? { repeatDay: request.repeatDay } : {}),
      };
      await onSave(request, update);
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : "Could not save scheduled meal.");
    }
  });
  const title = plan ? "Edit scheduled meal" : "Add a meal";
  return <ScheduleDialog closeDisabled={pending} describedBy="schedule-editor-description" labelledBy="schedule-editor-title" onClose={onClose} triggerRef={triggerRef}>
    <form aria-describedby="schedule-editor-description" onSubmit={submit}>
      <header className="dialog__heading"><h2 id="schedule-editor-title">{title}</h2><button aria-label="Close schedule editor" disabled={pending} onClick={onClose} type="button">×</button></header>
      <p id="schedule-editor-description">Changes are sent to your feeder and saved only after its confirmation.</p>
      <div className="form-grid">
        <label className="field" htmlFor="schedule-time">Time<input aria-invalid={Boolean(form.formState.errors.executionTime)} id="schedule-time" type="time" {...form.register("executionTime", { pattern: { value: /^(?:[01]\d|2[0-3]):[0-5]\d$/, message: "Enter a valid time." }, required: "Time is required." })} /></label>
        <label className="field" htmlFor="schedule-portions">Portions<input aria-invalid={Boolean(form.formState.errors.grainNum)} id="schedule-portions" max={48} min={1} type="number" {...form.register("grainNum", { max: { value: 48, message: "Maximum is 48 portions." }, min: { value: 1, message: "Minimum is 1 portion." }, required: true, valueAsNumber: true })} /></label>
      </div>
      <label className="switch-row" htmlFor="schedule-sound"><span>Play a sound with this meal</span><input id="schedule-sound" type="checkbox" {...form.register("enableAudio")} /></label>
      <label className="field" htmlFor="schedule-audio-times">Sound repetitions<input aria-invalid={Boolean(form.formState.errors.audioTimes)} disabled={!form.watch("enableAudio")} id="schedule-audio-times" max={5} min={1} type="number" {...form.register("audioTimes", { max: { value: 5, message: "Maximum is 5 repetitions." }, min: { value: 1, message: "Minimum is 1 repetition." }, valueAsNumber: true })} /></label>
      <label className="field" htmlFor="schedule-repeat">Repeats<select id="schedule-repeat" {...form.register("repeatMode")}><option value="every">Every day</option><option value="custom">Selected days</option><option value="never">Disabled</option></select></label>
      {repeatMode === "custom" && <fieldset className="schedule-days"><legend>Days</legend><div>{SCHEDULE_DAYS.map(({ label, value }) => <label key={value}><input type="checkbox" value={value} {...form.register("repeatDay", { valueAsNumber: true })} />{label}</label>)}</div>{form.formState.errors.repeatDay && <p className="form-error" role="alert">{form.formState.errors.repeatDay.message}</p>}</fieldset>}
      {(submitError || form.formState.errors.executionTime?.message || form.formState.errors.grainNum?.message || form.formState.errors.audioTimes?.message) && <p className="form-error" role="alert">{submitError ?? form.formState.errors.executionTime?.message ?? form.formState.errors.grainNum?.message ?? form.formState.errors.audioTimes?.message}</p>}
      <footer><button disabled={pending} onClick={onClose} type="button">Cancel</button><button className="primary-button" disabled={pending || saveDisabled || (Boolean(plan) && !form.formState.isDirty)} type="submit">{pending ? "Saving…" : "Save meal"}</button></footer>
    </form>
  </ScheduleDialog>;
}
