import { useEffect, type JSX } from "react";
import { useLocation, useParams } from "react-router-dom";

const REACT_TABS = new Set(["activity", "advanced", "camera", "schedule", "settings"]);
const DEVICE_TABS = new Set(["overview", ...REACT_TABS]);

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
    window.location.replace(REACT_TABS.has(tab) ? `/devices/${encodeURIComponent(deviceId)}/${tab}` : legacyDeviceUrl(deviceId, tab));
  }, [deviceId, tab]);
  return <p className="state-message">Opening the classic feeder view…</p>;
}

export function LegacyGlobalRedirect({ path }: { path: string }): JSX.Element {
  useEffect(() => { window.location.replace(`/${path}?ui=legacy`); }, [path]);
  return <p className="state-message">Opening the classic settings view…</p>;
}
