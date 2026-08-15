import { useCallback, useEffect, useRef, useState, type JSX } from "react";
import { useQuery } from "@tanstack/react-query";

import { getHome } from "../../api/devices";
import { queryKeys } from "../../api/queryKeys";
import type { DailyDevice } from "../../types/api";
import { StatusBadge } from "../../components/StatusBadge";
import { CameraPlayer } from "../camera/CameraPlayer";
import { DispenseDialog } from "../devices/DispenseDialog";

const HOME_PREVIEW_ROOT_MARGIN = "0px";
const HOME_REFRESH_MS = 3_000;

function todaySchedules(device: DailyDevice): DailyDevice["schedule"] {
  const mondayBasedDay = ((new Date().getDay() + 6) % 7) + 1;
  return device.schedule.filter((plan) => plan.repeat_day.includes(mondayBasedDay));
}

function wifiLabel(rssi: number | null): string {
  if (rssi === null) return "Wi-Fi unknown";
  if (rssi > -50) return "Wi-Fi excellent";
  if (rssi > -60) return "Wi-Fi good";
  if (rssi > -70) return "Wi-Fi fair";
  return "Wi-Fi weak";
}

interface DeviceCardProps {
  device: DailyDevice;
  onVisibilityChange: (deviceId: string, visible: boolean) => void;
  previewActive: boolean;
}

function DeviceCard({ device, onVisibilityChange, previewActive }: DeviceCardProps): JSX.Element {
  const cardRef = useRef<HTMLElement>(null);
  const dispenseTriggerRef = useRef<HTMLButtonElement>(null);
  const [dispenseOpen, setDispenseOpen] = useState(false);
  const closeDispense = useCallback((): void => setDispenseOpen(false), []);
  const online = device.local_state === "LOCAL_ONLINE";
  const cameraAvailable = device.camera.bridge_registered && device.camera.go2rtc_reachable && device.camera.bridge_reachable !== false;
  const scheduledToday = todaySchedules(device);
  useEffect(() => {
    const element = cardRef.current;
    if (element === null) return undefined;
    if (!("IntersectionObserver" in window)) {
      onVisibilityChange(device.device_id, true);
      return () => onVisibilityChange(device.device_id, false);
    }
    const observer = new IntersectionObserver(
      (entries) => onVisibilityChange(device.device_id, entries.some((entry) => entry.isIntersecting)),
      { rootMargin: HOME_PREVIEW_ROOT_MARGIN, threshold: 0.01 },
    );
    observer.observe(element);
    return () => {
      observer.disconnect();
      onVisibilityChange(device.device_id, false);
    };
  }, [device.device_id, onVisibilityChange]);
  return (
    <article className="device-card" ref={cardRef}>
      <header className="device-card__header">
        <div>
          <h2>{device.product_id ?? "PETLIBRO feeder"}</h2>
          <div className="status-row">
            <StatusBadge tone={online ? "online" : "offline"}>{online ? "Online" : "Offline"}</StatusBadge>
            <StatusBadge tone="neutral">{wifiLabel(device.rssi)}</StatusBadge>
          </div>
        </div>
        <a aria-label="Open feeder settings" className="icon-link" href={`/devices/${encodeURIComponent(device.device_id)}/settings`}>⚙</a>
      </header>
      {cameraAvailable && previewActive
        ? <CameraPlayer deviceId={device.device_id} compact />
        : <section className="camera-placeholder"><strong>{cameraAvailable ? "Camera preview paused" : "Camera unavailable"}</strong><span>{cameraAvailable ? "Scroll this feeder into view to start live video." : device.camera.reason ?? "Waiting for the local camera connection."}</span></section>}
      <section className="schedule-summary">
        <h3>Today’s schedule</h3>
        {scheduledToday.length === 0 ? <p>No meal planned today.</p> : <ul>{scheduledToday.map((plan) => <li key={`${plan.execution_time}-${plan.grain_num}`}>○ {plan.execution_time} · {plan.grain_num} portions</li>)}</ul>}
      </section>
      <footer className="device-card__footer"><button className="primary-button" disabled={!online} onClick={() => setDispenseOpen(true)} ref={dispenseTriggerRef} type="button">Dispense now</button></footer>
      {dispenseOpen && <DispenseDialog deviceId={device.device_id} onClose={closeDispense} triggerRef={dispenseTriggerRef} />}
    </article>
  );
}

export function HomePage(): JSX.Element {
  const [visibleDeviceIds, setVisibleDeviceIds] = useState<ReadonlySet<string>>(new Set());
  const [previewDeviceId, setPreviewDeviceId] = useState<string | null>(null);
  const home = useQuery({ queryKey: queryKeys.home, queryFn: ({ signal }) => getHome(signal), refetchInterval: HOME_REFRESH_MS });
  const onVisibilityChange = useCallback((deviceId: string, visible: boolean): void => {
    setVisibleDeviceIds((current) => {
      if (current.has(deviceId) === visible) return current;
      const next = new Set(current);
      if (visible) next.add(deviceId); else next.delete(deviceId);
      return next;
    });
  }, []);
  useEffect(() => {
    const availableVisibleIds = home.data?.devices
      .filter((device) => visibleDeviceIds.has(device.device_id) && device.camera.bridge_registered && device.camera.go2rtc_reachable && device.camera.bridge_reachable !== false)
      .map((device) => device.device_id) ?? [];
    setPreviewDeviceId((current) => current !== null && availableVisibleIds.includes(current) ? current : availableVisibleIds[0] ?? null);
  }, [home.data?.devices, visibleDeviceIds]);
  if (!home.data && home.isPending) return <p className="state-message">Loading feeders…</p>;
  if (!home.data && home.isError) return <p className="state-message state-message--error">Unable to reach the relay: {home.error.message}</p>;
  return <section aria-labelledby="home-title"><header className="page-heading"><div><h1 id="home-title">Your feeders</h1><p>At-a-glance local status and today’s meals.</p>{home.isError && <p className="refresh-warning" role="status">Updating feeder status failed. Live video is unchanged.</p>}</div></header>{home.data!.devices.length === 0 ? <p className="state-message">No local feeders are available yet.</p> : <div className="device-grid">{home.data!.devices.map((device) => <DeviceCard device={device} key={device.device_id} onVisibilityChange={onVisibilityChange} previewActive={previewDeviceId === device.device_id} />)}</div>}</section>;
}
