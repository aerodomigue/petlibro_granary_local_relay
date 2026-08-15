import { useEffect, type JSX } from "react";
import { useLocation, useParams } from "react-router-dom";

const DEVICE_TABS = new Set(["overview", "camera", "activity", "settings", "advanced"]);

export function legacyDeviceUrl(deviceId: string, tab: string): string {
  return `/devices/${encodeURIComponent(deviceId)}?ui=legacy#${encodeURIComponent(tab)}`;
}

export function LegacyDeviceRedirect(): JSX.Element {
  const { deviceId, tab: routeTab } = useParams();
  const { hash } = useLocation();
  const requestedTab = routeTab ?? hash.slice(1);
  const tab = requestedTab === "schedule" ? "schedule" : DEVICE_TABS.has(requestedTab) ? requestedTab : "overview";
  useEffect(() => {
    if (!deviceId) return;
    window.location.replace(tab === "schedule" ? `/devices/${encodeURIComponent(deviceId)}/schedule` : legacyDeviceUrl(deviceId, tab));
  }, [deviceId, tab]);
  return <p className="state-message">Opening the classic feeder view…</p>;
}

export function LegacyGlobalRedirect({ path }: { path: string }): JSX.Element {
  useEffect(() => { window.location.replace(`/${path}?ui=legacy`); }, [path]);
  return <p className="state-message">Opening the classic settings view…</p>;
}
