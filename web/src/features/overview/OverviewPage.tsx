import { useCallback, useRef, useState, type JSX } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { getDailyDevice } from "../../api/deviceDetails";
import { queryKeys } from "../../api/queryKeys";
import { DeviceNavigation } from "../../components/DeviceNavigation";
import { StatusBadge } from "../../components/StatusBadge";
import { TodaySchedule } from "../../components/TodaySchedule";
import { usePreferences } from "../../preferences/PreferencesContext";
import { lastSeenLabel, wifiLabel } from "../devices/presentation";
import { DispenseDialog } from "../devices/DispenseDialog";

const OVERVIEW_REFRESH_MS = 3_000;

/** Render the everyday feeder summary from the safe daily state projection. */
export function OverviewPage(): JSX.Element {
  const { deviceId } = useParams();
  const { deviceNames } = usePreferences();
  const dispenseTriggerRef = useRef<HTMLButtonElement>(null);
  const [dispenseOpen, setDispenseOpen] = useState(false);
  const detail = useQuery({
    enabled: Boolean(deviceId),
    queryKey: queryKeys.dailyDevice(deviceId ?? ""),
    queryFn: ({ signal }) => getDailyDevice(deviceId ?? "", signal),
    refetchInterval: OVERVIEW_REFRESH_MS,
  });
  const closeDispense = useCallback((): void => setDispenseOpen(false), []);

  if (!deviceId) return <p className="state-message state-message--error">Unknown feeder.</p>;
  if (!detail.data && detail.isPending) return <p className="state-message">Loading feeder…</p>;
  if (!detail.data && detail.isError) return <p className="state-message state-message--error">Feeder overview is unavailable: {detail.error.message}</p>;

  const { camera, device } = detail.data!;
  const online = device.local_state === "LOCAL_ONLINE";
  const displayName = deviceNames[deviceId]?.trim() || device.product_id || "PETLIBRO feeder";
  const cameraAvailable = camera.bridge_registered && camera.go2rtc_reachable && camera.bridge_reachable !== false;

  return <section aria-labelledby="overview-title">
    <header className="page-heading">
      <div>
        <Link to="/">← All feeders</Link>
        <h1 id="overview-title">{displayName}</h1>
        <div className="status-row">
          <StatusBadge tone={online ? "online" : "offline"}>{online ? "Online" : "Offline"}</StatusBadge>
          <StatusBadge tone="neutral">{wifiLabel(device.rssi)}</StatusBadge>
        </div>
        <p>{lastSeenLabel(device.last_seen_at)}</p>
        {detail.isError && <p className="refresh-warning" role="status">Updating feeder status failed. Showing the most recent available details.</p>}
      </div>
    </header>
    <DeviceNavigation active="overview" deviceId={deviceId} />
    <div className="overview-grid">
      <article className="overview-card">
        <TodaySchedule schedule={device.schedule} />
        <Link className="text-button" to={`/devices/${encodeURIComponent(deviceId)}/schedule`}>Manage schedule</Link>
      </article>
      <article className="overview-card overview-card--actions">
        <h2>Quick actions</h2>
        <p>Dispensing is sent to your feeder now and requires its confirmation.</p>
        <button className="primary-button" disabled={!online} onClick={() => setDispenseOpen(true)} ref={dispenseTriggerRef} type="button">Dispense now</button>
      </article>
      <article className="overview-card">
        <h2>Camera</h2>
        <p>{cameraAvailable ? "Camera is ready to view." : "Camera is unavailable right now."}</p>
        <Link className="button button--secondary" to={`/devices/${encodeURIComponent(deviceId)}/camera`}>{cameraAvailable ? "Open camera" : "View camera status"}</Link>
      </article>
    </div>
    {dispenseOpen && <DispenseDialog deviceId={deviceId} onClose={closeDispense} triggerRef={dispenseTriggerRef} />}
  </section>;
}
