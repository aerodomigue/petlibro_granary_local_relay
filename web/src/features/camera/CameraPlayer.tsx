import { useRef, type JSX } from "react";

import { useCameraViewer } from "./useCameraViewer";

interface CameraPlayerProps {
  deviceId: string;
  compact?: boolean;
}

/** Render one live feeder player while its lifecycle stays independent from polling. */
export function CameraPlayer({ deviceId, compact = false }: CameraPlayerProps): JSX.Element {
  const videoRef = useRef<HTMLVideoElement>(null);
  const { reconnect, status } = useCameraViewer(deviceId, videoRef);
  const label = status === "live" ? "Live" : status === "paused" ? "Camera paused" : status === "reconnecting" ? "Reconnecting…" : status === "error" ? "Camera unavailable" : "Connecting…";
  const canReconnect = status === "paused" || status === "error";
  return <section className={`camera-player ${compact ? "camera-player--compact" : ""}`} aria-label="Live feeder camera"><video ref={videoRef} autoPlay muted playsInline /><span aria-live="polite" className={`camera-player__status camera-player__status--${status}`}>{label}</span>{canReconnect && <button className="camera-player__reconnect" onClick={reconnect} type="button">Reconnect camera</button>}</section>;
}
