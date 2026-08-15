import type { JSX } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { getAdvancedDevice } from "../../api/deviceDetails";
import { queryKeys } from "../../api/queryKeys";
import { DeviceNavigation } from "../../components/DeviceNavigation";
import { usePreferences } from "../../preferences/PreferencesContext";

const ADVANCED_REFRESH_MS = 10_000;
function display(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Not available";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return value.length === 0 ? "None" : value.map(display).join(", ");
  if (typeof value === "object") return Object.entries(value).map(([key, nested]) => `${formatLabel(key)}: ${display(nested)}`).join(" · ");
  return String(value);
}

function formatLabel(value: string): string {
  return value.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/_/g, " ").replace(/^./, (first) => first.toUpperCase());
}

function DetailList({ entries }: { entries: ReadonlyArray<readonly [string, unknown]> }): JSX.Element {
  return <dl className="advanced-values">{entries.map(([label, value]) => <div key={label}><dt>{formatLabel(label)}</dt><dd>{display(value)}</dd></div>)}</dl>;
}

/** Gate technical diagnostics behind an explicit browser-local Advanced preference. */
export function AdvancedPage(): JSX.Element {
  const { deviceId } = useParams();
  const { advancedMode } = usePreferences();
  const detail = useQuery({ enabled: Boolean(deviceId) && advancedMode, queryKey: queryKeys.advanced(deviceId ?? ""), queryFn: ({ signal }) => getAdvancedDevice(deviceId ?? "", signal), refetchInterval: ADVANCED_REFRESH_MS });
  if (!deviceId) return <Navigate replace to="/" />;
  if (!advancedMode) {
    return <section className="state-message" aria-labelledby="advanced-disabled-title"><h1 id="advanced-disabled-title">Advanced mode is off</h1><p>Enable Advanced mode in global settings to view technical relay diagnostics.</p><Link className="button button--secondary" to="/settings">Open settings</Link></section>;
  }
  if (!detail.data && detail.isPending) return <p className="state-message">Loading diagnostics…</p>;
  if (!detail.data && detail.isError) return <p className="state-message state-message--error">Diagnostics are unavailable: {detail.error.message}</p>;
  const device = detail.data!;
  return <section aria-labelledby="advanced-title"><header className="page-heading"><div><h1 id="advanced-title">Advanced diagnostics</h1><p>Technical relay information. Credentials are never displayed.</p>{detail.isError && <p className="refresh-warning" role="status">Updating diagnostics failed. Showing the last available values.</p>}</div></header><DeviceNavigation active="advanced" deviceId={deviceId} /><div className="advanced-grid"><details className="advanced-section" open><summary>Device</summary><DetailList entries={[["Model", device.device.product_id], ["Firmware", device.device.firmware], ["Device ID", device.device.device_id], ["MAC", device.device.mac], ["IP address", device.device.ip], ["RSSI", device.device.rssi]]} /></details><details className="advanced-section"><summary>Connectivity and cloud</summary><DetailList entries={[["Local state", device.device.local_state], ["PETLIBRO state", device.device.cloud_state], ...Object.entries(device.connectivity)]} /></details><details className="advanced-section"><summary>Camera</summary><DetailList entries={Object.entries(device.camera)} /></details><details className="advanced-section"><summary>Relay</summary><DetailList entries={Object.entries(device.relay)} /></details><details className="advanced-section"><summary>State summary</summary><DetailList entries={Object.entries(device.state_summary)} /></details><details className="advanced-section"><summary>Recent device logs</summary>{device.logs.length === 0 ? <p>No recent device logs.</p> : <ol className="advanced-logs">{device.logs.map((entry, index) => <li key={`${entry.timestamp ?? "unknown"}-${index}`}><strong>{entry.level} · {entry.component}</strong><span>{entry.message}</span></li>)}</ol>}</details></div></section>;
}
