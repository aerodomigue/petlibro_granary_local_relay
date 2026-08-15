import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type JSX, type SetStateAction } from "react";
import { useForm } from "react-hook-form";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import { getDailyDevice, updateControlGroup, type ControlGroup, type ControlUpdate } from "../../api/deviceDetails";
import { queryKeys } from "../../api/queryKeys";
import { DeviceNavigation } from "../../components/DeviceNavigation";
import { usePreferences } from "../../preferences/PreferencesContext";
import type { ControlCapability, SettingValue } from "../../types/api";
import { DEVICE_SETTING_GROUPS, type DeviceSettingFormValues, type DeviceSettingGroup, type SettingField } from "./deviceSettings";

const SETTINGS_REFRESH_MS = 3_000;

function initialValues(group: DeviceSettingGroup, values: Readonly<Record<string, SettingValue>>): DeviceSettingFormValues {
  return Object.fromEntries(group.fields.map((field) => [field.key, values[field.key]]));
}

function updatedValues(group: DeviceSettingGroup, values: DeviceSettingFormValues, dirty: Record<string, unknown>): ControlUpdate {
  const fields = new Map(group.fields.map((field) => [field.key, field]));
  return Object.fromEntries(Object.keys(dirty).flatMap((key) => {
    const field = fields.get(key);
    const value = values[key];
    if (!field || value === undefined) return [];
    if ((field.type === "time" || field.type === "select") && value === "") return [];
    if (field.type === "number") {
      const numeric = Number(value);
      return Number.isFinite(numeric) ? [[key, numeric] as const] : [];
    }
    if (field.type === "select" && key.endsWith("AgingType")) {
      const numeric = Number(value);
      return Number.isFinite(numeric) ? [[key, numeric] as const] : [];
    }
    return [[key, value] as const];
  })) as ControlUpdate;
}

function unavailableReason(capability: ControlCapability | undefined): string {
  if (!capability?.writable) return "This feeder setting is not supported.";
  if (!capability.device_online) return "This setting is unavailable while the feeder is offline.";
  if (!capability.required_state_available) return "Waiting for the feeder state required to update this setting.";
  if (capability.pending) return "Another feeder change is awaiting confirmation.";
  return "Changes are sent to the feeder and saved after its confirmation.";
}

function SettingInput({ field, form }: { field: SettingField; form: ReturnType<typeof useForm<DeviceSettingFormValues>> }): JSX.Element {
  const inputId = `setting-${field.key}`;
  if (field.type === "boolean") return <label className="switch-row" htmlFor={inputId}><span>{field.label}</span><input id={inputId} role="switch" type="checkbox" {...form.register(field.key)} /></label>;
  if (field.type === "select") return <label className="field" htmlFor={inputId}>{field.label}<select id={inputId} {...form.register(field.key)}><option value="">Not set</option>{field.options.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>;
  if (field.type === "time") return <label className="field" htmlFor={inputId}>{field.label}<input id={inputId} type="time" {...form.register(field.key)} /></label>;
  return <label className="field" htmlFor={inputId}>{field.label}<input id={inputId} max={field.max} min={field.min} type="number" {...form.register(field.key, { valueAsNumber: true })} /></label>;
}

function SettingGroupCard({ group, values, capability, deviceId, savingGroup, setSavingGroup, onAcknowledged }: { group: DeviceSettingGroup; values: Readonly<Record<string, SettingValue>>; capability: ControlCapability | undefined; deviceId: string; savingGroup: ControlGroup | null; setSavingGroup: Dispatch<SetStateAction<ControlGroup | null>>; onAcknowledged: (update: ControlUpdate) => void }): JSX.Element {
  const queryClient = useQueryClient();
  const [feedback, setFeedback] = useState<string | null>(null);
  const form = useForm<DeviceSettingFormValues>({ defaultValues: initialValues(group, values) });
  const serverSnapshot = useMemo(() => initialValues(group, values), [group, values]);
  const serverSnapshotKey = useMemo(() => JSON.stringify(serverSnapshot), [serverSnapshot]);
  const lastServerSnapshotKey = useRef(serverSnapshotKey);
  const mutation = useMutation({ mutationFn: (update: ControlUpdate) => updateControlGroup(deviceId, group.route as ControlGroup, update) });
  const unavailable = !capability?.writable || !capability.device_online || !capability.required_state_available || capability.pending;
  const saving = savingGroup !== null;
  useEffect(() => {
    if (lastServerSnapshotKey.current === serverSnapshotKey) return;
    lastServerSnapshotKey.current = serverSnapshotKey;
    for (const field of group.fields) {
      if (!form.getFieldState(field.key).isDirty) {
        form.setValue(field.key, serverSnapshot[field.key], { shouldDirty: false });
      }
    }
  }, [form, group, serverSnapshot, serverSnapshotKey]);
  const submit = form.handleSubmit(async (draft) => {
    const update = updatedValues(group, draft, form.formState.dirtyFields);
    if (Object.keys(update).length === 0 || saving) return;
    setSavingGroup(group.route);
    setFeedback(null);
    try {
      await mutation.mutateAsync(update);
      form.reset({ ...initialValues(group, values), ...update });
      onAcknowledged(update);
      setFeedback("Saved. Feeder confirmed the change.");
      await Promise.all([queryClient.invalidateQueries({ queryKey: queryKeys.settings(deviceId) }), queryClient.invalidateQueries({ queryKey: queryKeys.home })]);
    } catch (error) {
      setFeedback(error instanceof Error ? `Unable to save: ${error.message}` : "Unable to save this setting.");
    } finally {
      setSavingGroup(null);
    }
  });
  return <article className="settings-card"><h2>{group.title}</h2><form onSubmit={submit}><fieldset disabled={unavailable || saving}><div className="settings-fields">{group.fields.map((field) => <SettingInput field={field} form={form} key={field.key} />)}</div></fieldset><footer className="settings-card__footer"><p aria-live="polite" className={mutation.isError ? "form-error" : "muted"}>{feedback ?? unavailableReason(capability)}</p><button aria-label={`Save ${group.title.toLowerCase()} settings`} className="primary-button settings-card__save" disabled={unavailable || saving || !form.formState.isDirty} type="submit">{mutation.isPending ? "Saving…" : "Save"}</button></footer></form></article>;
}

function FeederNameCard({ deviceId }: { deviceId: string }): JSX.Element {
  const { deviceNames, setDeviceName } = usePreferences();
  const [saved, setSaved] = useState(false);
  const form = useForm<{ name: string }>({ defaultValues: { name: deviceNames[deviceId] ?? "" } });
  return <article className="settings-card"><h2>Feeder name</h2><p>Shown only in this browser. It does not change your PETLIBRO account.</p><form onSubmit={form.handleSubmit(({ name }) => { const normalizedName = name.trim(); setDeviceName(deviceId, normalizedName); form.reset({ name: normalizedName }); setSaved(true); })}><label className="field" htmlFor="feeder-name">Name<input id="feeder-name" maxLength={60} {...form.register("name")} /></label><footer className="settings-card__footer"><p aria-live="polite" className="muted">{saved ? "Saved in this browser." : ""}</p><button className="primary-button settings-card__save" disabled={!form.formState.isDirty} type="submit">Save name</button></footer></form></article>;
}

/** Render feeder-confirmed settings without allowing query refetches to overwrite dirty forms. */
export function DeviceSettingsPage(): JSX.Element {
  const { deviceId } = useParams();
  const [savingGroup, setSavingGroup] = useState<ControlGroup | null>(null);
  const [acknowledgedValues, setAcknowledgedValues] = useState<Readonly<Record<string, SettingValue>>>({});
  const detail = useQuery({ enabled: Boolean(deviceId), queryKey: queryKeys.settings(deviceId ?? ""), queryFn: ({ signal }) => getDailyDevice(deviceId ?? "", signal), refetchInterval: SETTINGS_REFRESH_MS });
  const serverValues = useMemo(
    () => detail.data === undefined
      ? {}
      : Object.fromEntries([...detail.data.state.desired, ...detail.data.state.local_confirmed].map(({ key, value }) => [key, value])),
    [detail.data],
  );
  useEffect(() => {
    setAcknowledgedValues((current) => Object.fromEntries(Object.entries(current).filter(([key, value]) => serverValues[key] !== value)));
  }, [serverValues]);
  const values = useMemo(() => ({ ...serverValues, ...acknowledgedValues }), [acknowledgedValues, serverValues]);
  const acknowledge = useCallback((update: ControlUpdate): void => {
    setAcknowledgedValues((current) => ({ ...current, ...update }));
  }, []);
  if (!deviceId) return <p className="state-message state-message--error">Unknown feeder.</p>;
  if (!detail.data && detail.isPending) return <p className="state-message">Loading feeder settings…</p>;
  if (!detail.data && detail.isError) return <p className="state-message state-message--error">Settings are unavailable: {detail.error.message}</p>;
  const supportedGroups = DEVICE_SETTING_GROUPS.filter((group) => detail.data!.controls[group.primary] !== undefined);
  return <section aria-labelledby="device-settings-title"><header className="page-heading"><div><h1 id="device-settings-title">Feeder settings</h1><p>Everyday preferences saved directly by your feeder.</p>{detail.isError && <p className="refresh-warning" role="status">Updating feeder settings failed. Showing the last confirmed values.</p>}</div></header><DeviceNavigation active="settings" deviceId={deviceId} /><div className="settings-grid"><FeederNameCard deviceId={deviceId} key={deviceId} />{supportedGroups.map((group) => <SettingGroupCard capability={detail.data!.controls[group.primary]} deviceId={deviceId} group={group} key={`${deviceId}-${group.route}`} onAcknowledged={acknowledge} savingGroup={savingGroup} setSavingGroup={setSavingGroup} values={values} />)}</div></section>;
}
