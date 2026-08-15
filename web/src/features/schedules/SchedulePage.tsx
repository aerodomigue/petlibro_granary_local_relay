import { useCallback, useRef, useState, type JSX, type RefObject } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { createSchedule, deleteSchedule, getSchedules, updateSchedule } from "../../api/schedules";
import { queryKeys } from "../../api/queryKeys";
import type { Schedule, ScheduleCreateRequest, ScheduleFormValues, ScheduleSnapshot, ScheduleUpdateRequest } from "../../types/api";
import { DeviceNavigation } from "../../components/DeviceNavigation";
import { ScheduleDeleteDialog } from "./ScheduleDeleteDialog";
import { ScheduleEditor } from "./ScheduleEditor";
import { ScheduleList } from "./ScheduleList";
import { scheduleToFormValues } from "./scheduleForm";

const SCHEDULE_REFRESH_MS = 3_000;

type EditorState = { initialError?: string; initialValues?: ScheduleFormValues; plan?: Schedule; triggerRef: RefObject<HTMLElement | null> } | null;
type MutationCommand =
  | { kind: "create"; values: ScheduleCreateRequest }
  | { kind: "update"; planId: number; values: ScheduleUpdateRequest }
  | { kind: "delete"; planId: number };

function orderedSchedules(entries: ScheduleSnapshot[]): ScheduleSnapshot[] {
  return [...entries].sort((first, second) => first.plan.executionTime.localeCompare(second.plan.executionTime));
}

/** Render the feeder-confirmed Schedule route without sharing mutable form state with Query. */
export function SchedulePage(): JSX.Element {
  const { deviceId } = useParams();
  const queryClient = useQueryClient();
  const addTriggerRef = useRef<HTMLButtonElement>(null);
  const activeMutationRef = useRef(false);
  const disabledDaysRef = useRef(new Map<number, Schedule["repeatDay"]>());
  const [editor, setEditor] = useState<EditorState>(null);
  const [deleting, setDeleting] = useState<{ entry: ScheduleSnapshot; triggerRef: RefObject<HTMLElement | null> } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [refreshingMutation, setRefreshingMutation] = useState(false);
  const schedules = useQuery({
    enabled: Boolean(deviceId),
    queryKey: queryKeys.schedule(deviceId ?? ""),
    queryFn: ({ signal }) => getSchedules(deviceId ?? "", signal),
    refetchInterval: SCHEDULE_REFRESH_MS,
  });
  const mutation = useMutation({
    mutationFn: async (command: MutationCommand) => {
      if (!deviceId) throw new Error("Unknown feeder.");
      if (command.kind === "create") return createSchedule(deviceId, command.values);
      if (command.kind === "update") return updateSchedule(deviceId, command.planId, command.values);
      return deleteSchedule(deviceId, command.planId);
    },
  });
  const runMutation = useCallback(async (command: MutationCommand): Promise<void> => {
    if (activeMutationRef.current) throw new Error("Another feeder change is still waiting for confirmation.");
    activeMutationRef.current = true;
    setActionError(null);
    try {
      await mutation.mutateAsync(command);
      setRefreshingMutation(true);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.schedule(deviceId ?? "") }),
        queryClient.invalidateQueries({ queryKey: queryKeys.home }),
      ]);
    } finally {
      activeMutationRef.current = false;
      setRefreshingMutation(false);
    }
  }, [deviceId, mutation, queryClient]);
  const openCreate = (): void => setEditor({ triggerRef: addTriggerRef });
  const openEdit = (entry: ScheduleSnapshot, trigger: HTMLButtonElement): void => {
    const triggerRef: RefObject<HTMLElement | null> = { current: trigger };
    setEditor({ plan: entry.plan, triggerRef });
  };
  const closeEditor = useCallback((): void => setEditor(null), []);
  const closeDelete = useCallback((): void => setDeleting(null), []);
  const saveEditor = async (plan: Schedule | undefined, values: ScheduleCreateRequest | ScheduleUpdateRequest): Promise<void> => {
    if (plan) await runMutation({ kind: "update", planId: plan.planId, values });
    else await runMutation({ kind: "create", values: values as ScheduleCreateRequest });
    closeEditor();
  };
  const disableOrEnable = async (entry: ScheduleSnapshot, trigger: HTMLButtonElement): Promise<void> => {
    setActionError(null);
    if (entry.plan.repeatDay.length > 0) {
      await runMutation({ kind: "update", planId: entry.plan.planId, values: { repeatDay: [] } });
      disabledDaysRef.current.set(entry.plan.planId, entry.plan.repeatDay);
      return;
    }
    const previousDays = disabledDaysRef.current.get(entry.plan.planId);
    const initialValues = { ...scheduleToFormValues(entry.plan), repeatDay: previousDays ?? [], repeatMode: "custom" as const };
    setEditor({
      ...(previousDays && previousDays.length > 0 ? {} : { initialError: "Choose at least one day before enabling this meal." }),
      initialValues,
      plan: entry.plan,
      triggerRef: { current: trigger },
    });
  };
  const handleToggle = (entry: ScheduleSnapshot, trigger: HTMLButtonElement): void => {
    void disableOrEnable(entry, trigger).catch((error: unknown) => setActionError(error instanceof Error ? error.message : "Could not update scheduled meal."));
  };
  const deleteEntry = (entry: ScheduleSnapshot, trigger: HTMLButtonElement): void => setDeleting({ entry, triggerRef: { current: trigger } });
  const confirmDelete = async (): Promise<void> => {
    if (!deleting) return;
    await runMutation({ kind: "delete", planId: deleting.entry.plan.planId });
    disabledDaysRef.current.delete(deleting.entry.plan.planId);
    setDeleting(null);
  };
  if (!deviceId) return <p className="state-message state-message--error">Unknown feeder.</p>;
  if (!schedules.data && schedules.isPending) return <p className="state-message">Loading schedule…</p>;
  if (!schedules.data && schedules.isError) return <p className="state-message state-message--error">Schedule is unavailable: {schedules.error.message}</p>;
  const entries = orderedSchedules(schedules.data?.schedules ?? []);
  const feederOnline = schedules.data?.device.local_state === "LOCAL_ONLINE";
  const pending = mutation.isPending || refreshingMutation;
  const actionDisabled = pending || !feederOnline;
  return <section aria-labelledby="schedule-title">
    <header className="page-heading schedule-page-heading"><div><Link to={`/devices/${encodeURIComponent(deviceId)}/overview`}>← Feeder overview</Link><h1 id="schedule-title">Schedule</h1><p>Meals are sent directly to your feeder and saved after its confirmation.</p>{!feederOnline && <p className="refresh-warning" role="status">Feeder offline. Schedule changes are unavailable until it reconnects.</p>}{schedules.isError && <p className="refresh-warning" role="status">Could not refresh the schedule. Showing the last confirmed version. <button className="text-button" onClick={() => { void schedules.refetch(); }} type="button">Try again</button></p>}</div><button className="primary-button schedule-add-button" disabled={actionDisabled} onClick={openCreate} ref={addTriggerRef} type="button">+ Add a meal</button></header>
    <DeviceNavigation active="schedule" deviceId={deviceId} />
    {actionError && <p className="form-error schedule-action-error" role="alert">{actionError}</p>}
    <ScheduleList disabled={actionDisabled} entries={entries} onDelete={deleteEntry} onEdit={openEdit} onToggle={handleToggle} />
    {editor && <ScheduleEditor initialError={editor.initialError} initialValues={editor.initialValues} onClose={closeEditor} onSave={(values, changes) => saveEditor(editor.plan, editor.plan ? changes : values)} pending={mutation.isPending} plan={editor.plan} saveDisabled={actionDisabled} triggerRef={editor.triggerRef} />}
    {deleting && <ScheduleDeleteDialog onClose={closeDelete} onDelete={confirmDelete} pending={mutation.isPending} plan={deleting.entry.plan} submitDisabled={actionDisabled} triggerRef={deleting.triggerRef} />}
  </section>;
}
