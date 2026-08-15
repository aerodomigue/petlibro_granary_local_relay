import { useQuery } from "@tanstack/react-query";
import type { JSX } from "react";
import { Link } from "react-router-dom";

import { getHome } from "../../api/devices";
import { StatusBadge } from "../../components/StatusBadge";
import type { DailyDevice } from "../../types/api";

const HOME_REFRESH_MS = 3_000;

function wifiLabel(rssi: number | null): string {
  if (rssi === null) return "Wi-Fi unknown";
  if (rssi > -50) return "Wi-Fi excellent";
  if (rssi > -60) return "Wi-Fi good";
  if (rssi > -70) return "Wi-Fi fair";
  return "Wi-Fi weak";
}

function DeviceCard({ device }: { device: DailyDevice }): JSX.Element {
  const online = device.local_state === "LOCAL_ONLINE";
  return (
    <article className="device-card">
      <header className="device-card__header">
        <div>
          <h2>{device.product_id ?? "PETLIBRO feeder"}</h2>
          <div className="status-row">
            <StatusBadge tone={online ? "online" : "offline"}>{online ? "Online" : "Offline"}</StatusBadge>
            <StatusBadge tone="neutral">{wifiLabel(device.rssi)}</StatusBadge>
          </div>
        </div>
        <Link aria-label="Open feeder settings" className="icon-link" to={`/devices/${encodeURIComponent(device.device_id)}/overview`}>⚙</Link>
      </header>
      <section className="camera-placeholder" aria-label="Camera migration status">
        <strong>Camera player is being migrated</strong>
        <span>The legacy dashboard remains available during this transition.</span>
      </section>
      <section className="schedule-summary">
        <h3>Today’s schedule</h3>
        {device.schedule.length === 0 ? <p>No meal planned today.</p> : <ul>{device.schedule.map((plan) => <li key={`${plan.execution_time}-${plan.grain_num}`}>○ {plan.execution_time} · {plan.grain_num} portions</li>)}</ul>}
      </section>
    </article>
  );
}

export function HomePage(): JSX.Element {
  const home = useQuery({ queryKey: ["home"], queryFn: ({ signal }) => getHome(signal), refetchInterval: HOME_REFRESH_MS });
  if (home.isPending) return <p className="state-message">Loading feeders…</p>;
  if (home.isError) return <p className="state-message state-message--error">Unable to reach the relay: {home.error.message}</p>;
  return <section aria-labelledby="home-title"><header className="page-heading"><div><h1 id="home-title">Your feeders</h1><p>At-a-glance local status and today’s meals.</p></div></header><div className="device-grid">{home.data.devices.map((device) => <DeviceCard key={device.device_id} device={device} />)}</div></section>;
}
