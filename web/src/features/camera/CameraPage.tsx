import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import type { JSX } from "react";

import { getDailyDevice } from "../../api/devices";
import { queryKeys } from "../../api/queryKeys";
import { CameraPlayer } from "./CameraPlayer";

const CAMERA_REFRESH_MS = 3_000;

export function CameraPage(): JSX.Element {
  const { deviceId } = useParams();
  const camera = useQuery({
    enabled: Boolean(deviceId),
    queryKey: queryKeys.dailyDevice(deviceId ?? ""),
    queryFn: ({ signal }) => getDailyDevice(deviceId ?? "", signal),
    refetchInterval: CAMERA_REFRESH_MS,
  });
  if (!deviceId) return <p className="state-message state-message--error">Unknown feeder.</p>;
  if (camera.isPending) return <p className="state-message">Loading camera…</p>;
  if (camera.isError) return <p className="state-message state-message--error">Camera status is unavailable: {camera.error.message}</p>;
  const available = camera.data.camera.bridge_registered && camera.data.camera.go2rtc_reachable;
  return <section aria-labelledby="camera-title"><header className="page-heading"><div><Link to={`/devices/${encodeURIComponent(deviceId)}/overview`}>← Feeder overview</Link><h1 id="camera-title">Camera</h1><p>Live video from your feeder.</p></div></header>{available ? <CameraPlayer deviceId={deviceId} /> : <section className="camera-placeholder"><strong>Camera unavailable</strong><span>{camera.data.camera.reason ?? "Waiting for the local camera connection."}</span></section>}</section>;
}
