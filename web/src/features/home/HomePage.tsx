import { useCallback, useEffect, useRef, useState, type JSX } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { getHome } from "../../api/devices";
import { queryKeys } from "../../api/queryKeys";
import type { DailyDevice } from "../../types/api";
import { StatusBadge } from "../../components/StatusBadge";
import { TodaySchedule } from "../../components/TodaySchedule";
import { usePreferences } from "../../preferences/PreferencesContext";
import { CameraPlayer } from "../camera/CameraPlayer";
import { DispenseDialog } from "../devices/DispenseDialog";
import { wifiLabel } from "../devices/presentation";

const HOME_PREVIEW_ROOT_MARGIN = "0px";
const HOME_REFRESH_MS = 3_000;

interface DeviceCardProps {
  device: DailyDevice;
  displayName: string;
  onVisibilityChange: (deviceId: string, visibilityRatio: number) => void;
  previewActive: boolean;
}

function DeviceCard({ device, displayName, onVisibilityChange, previewActive }: DeviceCardProps): JSX.Element {
  const cardRef = useRef<HTMLElement>(null);
  const dispenseTriggerRef = useRef<HTMLButtonElement>(null);
  const [dispenseOpen, setDispenseOpen] = useState(false);
  const closeDispense = useCallback((): void => setDispenseOpen(false), []);
  const online = device.local_state === "LOCAL_ONLINE";
  const cameraAvailable = device.camera.bridge_registered && device.camera.go2rtc_reachable && device.camera.bridge_reachable !== false;
  useEffect(() => {
    const element = cardRef.current;
    if (element === null) return undefined;
    if (!("IntersectionObserver" in window)) {
      onVisibilityChange(device.device_id, 1);
      return () => onVisibilityChange(device.device_id, 0);
    }
    const observer = new IntersectionObserver(
      (entries) => onVisibilityChange(device.device_id, Math.max(...entries.map((entry) => entry.isIntersecting ? entry.intersectionRatio : 0))),
      { rootMargin: HOME_PREVIEW_ROOT_MARGIN, threshold: [0, 0.01, 0.25, 0.5, 0.75, 1] },
    );
    observer.observe(element);
    return () => {
      observer.disconnect();
      onVisibilityChange(device.device_id, 0);
    };
  }, [device.device_id, onVisibilityChange]);
  return (
    <article className="device-card" ref={cardRef}>
      <header className="device-card__header">
        <div>
          <h2>{displayName}</h2>
          <div className="status-row">
            <StatusBadge tone={online ? "online" : "offline"}>{online ? "Online" : "Offline"}</StatusBadge>
            <StatusBadge tone="neutral">{wifiLabel(device.rssi)}</StatusBadge>
          </div>
        </div>
        <Link aria-label="Open feeder settings" className="icon-link" to={`/devices/${encodeURIComponent(device.device_id)}/overview`}>⚙</Link>
      </header>
      {cameraAvailable && previewActive
        ? <CameraPlayer deviceId={device.device_id} compact />
        : <section className="camera-placeholder"><strong>{cameraAvailable ? "Camera preview paused" : "Camera unavailable"}</strong><span>{cameraAvailable ? "Scroll this feeder into view to start live video." : device.camera.reason ?? "Waiting for the local camera connection."}</span></section>}
      <TodaySchedule schedule={device.schedule} />
      <footer className="device-card__footer"><button className="primary-button" disabled={!online} onClick={() => setDispenseOpen(true)} ref={dispenseTriggerRef} type="button">Dispense now</button></footer>
      {dispenseOpen && <DispenseDialog deviceId={device.device_id} onClose={closeDispense} triggerRef={dispenseTriggerRef} />}
    </article>
  );
}

export function HomePage(): JSX.Element {
  const { deviceNames } = usePreferences();
  const [visibilityRatios, setVisibilityRatios] = useState<ReadonlyMap<string, number>>(new Map());
  const [previewDeviceId, setPreviewDeviceId] = useState<string | null>(null);
  const home = useQuery({ queryKey: queryKeys.home, queryFn: ({ signal }) => getHome(signal), refetchInterval: HOME_REFRESH_MS });
  const onVisibilityChange = useCallback((deviceId: string, visibilityRatio: number): void => {
    setVisibilityRatios((current) => {
      if (current.get(deviceId) === visibilityRatio) return current;
      const next = new Map(current);
      if (visibilityRatio > 0) next.set(deviceId, visibilityRatio); else next.delete(deviceId);
      return next;
    });
  }, []);
  useEffect(() => {
    const visibleDevices = home.data?.devices
      .filter((device) => (visibilityRatios.get(device.device_id) ?? 0) > 0)
      .sort((left, right) => (visibilityRatios.get(right.device_id) ?? 0) - (visibilityRatios.get(left.device_id) ?? 0))
      ?? [];
    setPreviewDeviceId((current) => {
      if (current !== null && visibleDevices.some((device) => device.device_id === current)) return current;
      return visibleDevices.find((device) => device.camera.bridge_registered && device.camera.go2rtc_reachable && device.camera.bridge_reachable !== false)?.device_id ?? null;
    });
  }, [home.data?.devices, visibilityRatios]);
  if (!home.data && home.isPending) return <p className="state-message">Loading feeders…</p>;
  if (!home.data && home.isError) return <p className="state-message state-message--error">Unable to reach the relay: {home.error.message}</p>;
  return <section aria-labelledby="home-title"><header className="page-heading"><div><h1 id="home-title">Your feeders</h1><p>At-a-glance local status and today’s meals.</p>{home.isError && <p className="refresh-warning" role="status">Updating feeder status failed. Live video is unchanged.</p>}</div></header>{home.data!.devices.length === 0 ? <p className="state-message">No local feeders are available yet.</p> : <div className="device-grid">{home.data!.devices.map((device) => <DeviceCard device={device} displayName={deviceNames[device.device_id]?.trim() || device.product_id || "PETLIBRO feeder"} key={device.device_id} onVisibilityChange={onVisibilityChange} previewActive={previewDeviceId === device.device_id} />)}</div>}</section>;
}
