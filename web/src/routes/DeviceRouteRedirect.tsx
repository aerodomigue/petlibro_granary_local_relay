import { Navigate, useLocation, useParams } from "react-router-dom";
import type { JSX } from "react";

const DEVICE_TABS = new Set(["overview", "camera", "schedule", "activity", "settings", "advanced"]);

/** Translate legacy hash bookmarks to their equivalent React device route. */
export function DeviceRouteRedirect(): JSX.Element {
  const { deviceId, tab: routeTab } = useParams();
  const { hash } = useLocation();
  const requestedTab = routeTab ?? hash.slice(1);
  const tab = DEVICE_TABS.has(requestedTab) ? requestedTab : "overview";
  if (!deviceId) return <Navigate replace to="/" />;
  return <Navigate replace to={`/devices/${encodeURIComponent(deviceId)}/${tab}`} />;
}
