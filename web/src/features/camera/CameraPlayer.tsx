import { useEffect, useRef, useState, type JSX } from "react";

import { activateViewer, exchangeWebRtc, heartbeatViewer, releaseViewer, releaseWebRtc } from "../../api/camera";

const HEARTBEAT_INTERVAL_MS = 5_000;
const ICE_GATHER_TIMEOUT_MS = 2_500;
const RETRY_DELAYS_MS = [1_000, 2_000, 5_000, 10_000];

type PlayerStatus = "connecting" | "live" | "reconnecting" | "error";

interface CameraPlayerProps {
  deviceId: string;
  compact?: boolean;
}

function viewerId(): string {
  return crypto.randomUUID().replaceAll("-", "");
}

async function completeOffer(peerConnection: RTCPeerConnection): Promise<string> {
  if (peerConnection.iceGatheringState === "complete") return peerConnection.localDescription?.sdp ?? "";
  return new Promise((resolve) => {
    const finish = (): void => {
      clearTimeout(timeout);
      peerConnection.removeEventListener("icegatheringstatechange", onChange);
      resolve(peerConnection.localDescription?.sdp ?? "");
    };
    const onChange = (): void => { if (peerConnection.iceGatheringState === "complete") finish(); };
    const timeout = window.setTimeout(finish, ICE_GATHER_TIMEOUT_MS);
    peerConnection.addEventListener("icegatheringstatechange", onChange);
  });
}

export function CameraPlayer({ deviceId, compact = false }: CameraPlayerProps): JSX.Element {
  const videoRef = useRef<HTMLVideoElement>(null);
  const generationRef = useRef(0);
  const [status, setStatus] = useState<PlayerStatus>("connecting");

  useEffect(() => {
    let stopped = false;
    let peerConnection: RTCPeerConnection | null = null;
    let controller: AbortController | null = null;
    let heartbeatTimer: number | null = null;
    let retryTimer: number | null = null;
    let activeViewerId: string | null = null;
    let activeSessionId: string | null = null;
    let retryCount = 0;

    const current = (generation: number): boolean => !stopped && generation === generationRef.current && !controller?.signal.aborted;
    const release = (): void => {
      if (heartbeatTimer !== null) window.clearInterval(heartbeatTimer);
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      if (activeSessionId) void releaseWebRtc(deviceId, activeSessionId).catch(() => undefined);
      if (activeViewerId) void releaseViewer(deviceId, activeViewerId).catch(() => undefined);
      peerConnection?.close();
      if (videoRef.current) videoRef.current.srcObject = null;
      peerConnection = null;
      activeSessionId = null;
      activeViewerId = null;
    };

    const connect = async (): Promise<void> => {
      const generation = ++generationRef.current;
      controller = new AbortController();
      activeViewerId = viewerId();
      setStatus(retryCount === 0 ? "connecting" : "reconnecting");
      try {
        await activateViewer(deviceId, activeViewerId, controller.signal);
        if (!current(generation)) return;
        heartbeatTimer = window.setInterval(() => {
          if (activeViewerId) void heartbeatViewer(deviceId, activeViewerId).catch(() => release());
        }, HEARTBEAT_INTERVAL_MS);
        peerConnection = new RTCPeerConnection({ bundlePolicy: "max-bundle" });
        const stream = new MediaStream();
        peerConnection.ontrack = (event) => {
          if (!current(generation)) return;
          stream.addTrack(event.track);
          if (videoRef.current) {
            videoRef.current.srcObject = stream;
            void videoRef.current.play().catch(() => undefined);
          }
        };
        peerConnection.onconnectionstatechange = () => {
          if (current(generation) && peerConnection?.connectionState === "failed") retry();
        };
        peerConnection.addTransceiver("video", { direction: "recvonly" });
        peerConnection.addTransceiver("audio", { direction: "recvonly" });
        await peerConnection.setLocalDescription(await peerConnection.createOffer());
        if (!current(generation)) return;
        const exchange = await exchangeWebRtc(deviceId, activeViewerId, await completeOffer(peerConnection), controller.signal);
        if (!current(generation)) return;
        activeSessionId = exchange.sessionId;
        await peerConnection.setRemoteDescription({ type: "answer", sdp: exchange.answer });
      } catch {
        if (current(generation)) retry();
      }
    };

    const retry = (): void => {
      if (stopped || retryTimer !== null) return;
      release();
      const delay = RETRY_DELAYS_MS[Math.min(retryCount++, RETRY_DELAYS_MS.length - 1)];
      retryTimer = window.setTimeout(() => { retryTimer = null; if (!stopped) void connect(); }, delay);
    };

    const video = videoRef.current;
    if (video) video.onloadeddata = () => { if (!stopped) setStatus("live"); };
    void connect();
    return () => { stopped = true; generationRef.current += 1; controller?.abort(); release(); };
  }, [deviceId]);

  return <section className={`camera-player ${compact ? "camera-player--compact" : ""}`} aria-label="Live feeder camera"><video ref={videoRef} autoPlay muted playsInline /><span className={`camera-player__status camera-player__status--${status}`}>{status === "live" ? "Live" : status === "reconnecting" ? "Reconnecting…" : "Connecting…"}</span></section>;
}
