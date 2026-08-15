import { Link } from "react-router-dom";
import type { JSX } from "react";

import { legacyDeviceUrl } from "../routes/LegacyDeviceRedirect";
import { usePreferences } from "../preferences/PreferencesContext";

type DeviceRoute = "activity" | "advanced" | "camera" | "schedule" | "settings";

interface DeviceNavigationProps {
  active: DeviceRoute;
  deviceId: string;
}

/** Keep React-owned device navigation consistent while Overview remains legacy. */
export function DeviceNavigation({ active, deviceId }: DeviceNavigationProps): JSX.Element {
  const { advancedMode } = usePreferences();
  const route = (tab: DeviceRoute): string => `/devices/${encodeURIComponent(deviceId)}/${tab}`;
  return <nav aria-label="Feeder navigation" className="device-route-nav">
    <a href={legacyDeviceUrl(deviceId, "overview")}>Overview</a>
    {(["camera", "schedule", "activity", "settings"] as const).map((tab) => tab === active
      ? <span aria-current="page" key={tab}>{tab[0]!.toUpperCase() + tab.slice(1)}</span>
      : <Link key={tab} to={route(tab)}>{tab[0]!.toUpperCase() + tab.slice(1)}</Link>)}
    {advancedMode && (active === "advanced" ? <span aria-current="page">Advanced</span> : <Link to={route("advanced")}>Advanced</Link>)}
  </nav>;
}
