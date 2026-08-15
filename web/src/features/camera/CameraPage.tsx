import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { useEffect, useState, type JSX } from "react";

import { getCameraStatus } from "../../api/devices";
import { queryKeys } from "../../api/queryKeys";
import { CameraPlayer } from "./CameraPlayer";
import { DeviceNavigation } from "../../components/DeviceNavigation";

const CAMERA_REFRESH_MS = 3_000;

export function CameraPage(): JSX.Element {
  const { deviceId } = useParams();
  const camera = useQuery({
    enabled: Boolean(deviceId),
    queryKey: queryKeys.camera(deviceId ?? ""),
    queryFn: ({ signal }) => getCameraStatus(deviceId ?? "", signal),
    refetchInterval: CAMERA_REFRESH_MS,
  });
  if (!deviceId) return <p className="state-message state-message--error">Unknown feeder.</p>;
  if (!camera.data && camera.isPending) return <p className="state-message">Loading camera…</p>;
  if (!camera.data && camera.isError) return <p className="state-message state-message--error">Camera status is unavailable: {camera.error.message}</p>;
  const availability = camera.data!;
  const available = availability.bridge_registered && availability.go2rtc_reachable && availability.bridge_reachable !== false;
  return <CameraPageContent available={available} availabilityReason={availability.reason} deviceId={deviceId} key={deviceId} refreshFailed={camera.isError} />;
}

function CameraPageContent({ available, availabilityReason, deviceId, refreshFailed }: { available: boolean; availabilityReason?: string; deviceId: string; refreshFailed: boolean }): JSX.Element {
  const [playerActivated, setPlayerActivated] = useState(available);
  useEffect(() => {
    if (available) setPlayerActivated(true);
  }, [available]);
  return <section aria-labelledby="camera-title"><header className="page-heading"><div><Link to={`/devices/${encodeURIComponent(deviceId)}/overview`}>← Feeder overview</Link><h1 id="camera-title">Camera</h1><p>Live video from your feeder.</p>{refreshFailed && <p className="refresh-warning" role="status">Updating camera status failed. Live video is unchanged.</p>}</div></header><DeviceNavigation active="camera" deviceId={deviceId} />{playerActivated ? <CameraPlayer deviceId={deviceId} /> : <section className="camera-placeholder"><strong>Camera unavailable</strong><span>{availabilityReason ?? "Waiting for the local camera connection."}</span></section>}</section>;
}
