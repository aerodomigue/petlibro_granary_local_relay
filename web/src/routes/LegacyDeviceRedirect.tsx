import { Navigate, useLocation, useParams } from "react-router-dom";
import type { JSX } from "react";

const DEVICE_TABS = new Set(["overview", "camera", "schedule", "activity", "settings", "advanced"]);

export function LegacyDeviceRedirect(): JSX.Element {
  const { deviceId } = useParams();
  const { hash } = useLocation();
  const tab = hash.slice(1);
  const target = DEVICE_TABS.has(tab) ? tab : "overview";
  return <Navigate replace to={`/devices/${encodeURIComponent(deviceId ?? "")}/${target}`} />;
}
