import { useEffect, useRef, useState, type JSX } from "react";

import { useCameraViewer } from "./useCameraViewer";

interface CameraPlayerProps {
  deviceId: string;
  compact?: boolean;
}

const CAMERA_VOLUME_STORAGE_KEY = "petlibro-camera-volume";

function initialVolume(): number {
  try {
    const stored = Number(window.localStorage.getItem(CAMERA_VOLUME_STORAGE_KEY));
    return Number.isFinite(stored) && stored >= 0 && stored <= 1 ? stored : 1;
  } catch {
    return 1;
  }
}

/** Render one live feeder player while its lifecycle stays independent from polling. */
export function CameraPlayer({ deviceId, compact = false }: CameraPlayerProps): JSX.Element {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [muted, setMuted] = useState(true);
  const [volume, setVolume] = useState(initialVolume);
  const { reconnect, status } = useCameraViewer(deviceId, videoRef);
  const label = status === "live" ? "Live" : status === "paused" ? "Camera paused" : status === "reconnecting" ? "Reconnecting…" : status === "error" ? "Camera unavailable" : "Connecting…";
  const canReconnect = status === "paused" || status === "error";
  useEffect(() => {
    if (videoRef.current === null) return;
    videoRef.current.muted = muted;
    videoRef.current.volume = volume;
  }, [muted, volume]);
  const updateVolume = (nextVolume: number): void => {
    setVolume(nextVolume);
    try { window.localStorage.setItem(CAMERA_VOLUME_STORAGE_KEY, String(nextVolume)); } catch { /* Playback remains usable when storage is unavailable. */ }
  };
  const requestFullscreen = (): void => {
    const player = videoRef.current?.closest(".camera-player");
    if (player instanceof HTMLElement && document.fullscreenElement === null) void player.requestFullscreen().catch(() => undefined);
    else if (document.fullscreenElement !== null) void document.exitFullscreen().catch(() => undefined);
  };
  return <section className={`camera-player ${compact ? "camera-player--compact" : ""}`} aria-label="Live feeder camera"><video ref={videoRef} autoPlay muted={muted} playsInline /><span aria-live="polite" className={`camera-player__status camera-player__status--${status}`}>{label}</span><div className="camera-player__controls" aria-label="Camera controls"><button aria-label={muted ? "Unmute camera" : "Mute camera"} className="camera-player__control" onClick={() => setMuted((current) => !current)} type="button">{muted ? "🔇" : "🔊"}</button><input aria-label="Camera volume" className="camera-player__volume" max="1" min="0" onChange={(event) => updateVolume(Number(event.target.value))} step="0.05" type="range" value={volume} /><button aria-label="Toggle camera fullscreen" className="camera-player__control" onClick={requestFullscreen} type="button">⛶</button></div>{canReconnect && <button className="camera-player__reconnect" onClick={reconnect} type="button">Reconnect camera</button>}</section>;
}
