import { useCallback, useEffect, useState, type RefObject } from "react";

import { WebRtcExchangeError, activateViewer, exchangeWebRtc, heartbeatViewer, releaseViewer, releaseWebRtc } from "../../api/camera";

const HEARTBEAT_INTERVAL_MS = 5_000;
const HEARTBEAT_TIMEOUT_MS = 4_000;
const HIDDEN_VIEWER_TIMEOUT_MS = 15_000;
const ICE_GATHER_TIMEOUT_MS = 2_500;
const FIRST_FRAME_TIMEOUT_MS = 10_000;
const DISCONNECTED_TIMEOUT_MS = 5_000;
const WHEP_TIMEOUT_MS = 7_500;
const RETRY_DELAYS_MS = [1_000, 2_000, 5_000, 10_000];

export type PlayerStatus = "connecting" | "live" | "paused" | "reconnecting" | "error";

export interface CameraViewerController {
  reconnect: () => void;
  status: PlayerStatus;
}

interface ActiveViewer {
  controller: AbortController;
  disconnectedTimer: number | null;
  firstFrameReceived: boolean;
  firstFrameTimer: number | null;
  generation: number;
  heartbeatController: AbortController | null;
  heartbeatInFlight: boolean;
  heartbeatTimer: number | null;
  loadedDataHandler: (() => void) | null;
  peerConnection: RTCPeerConnection | null;
  sessionId: string | null;
  viewerId: string;
}

interface TimedSignal {
  cleanup: () => void;
  signal: AbortSignal;
}

function createViewerId(): string {
  const generated = globalThis.crypto?.randomUUID?.();
  if (generated) return generated.replaceAll("-", "");
  return Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
}

function abortError(): DOMException {
  return new DOMException("Operation aborted", "AbortError");
}

function completeOffer(peerConnection: RTCPeerConnection, signal: AbortSignal): Promise<string> {
  if (signal.aborted) return Promise.reject(abortError());
  if (peerConnection.iceGatheringState === "complete") return Promise.resolve(peerConnection.localDescription?.sdp ?? "");
  return new Promise((resolve, reject) => {
    const finish = (): void => {
      clearTimeout(timeout);
      signal.removeEventListener("abort", onAbort);
      peerConnection.removeEventListener("icegatheringstatechange", onChange);
      resolve(peerConnection.localDescription?.sdp ?? "");
    };
    const onAbort = (): void => {
      clearTimeout(timeout);
      peerConnection.removeEventListener("icegatheringstatechange", onChange);
      reject(abortError());
    };
    const onChange = (): void => { if (peerConnection.iceGatheringState === "complete") finish(); };
    const timeout = window.setTimeout(finish, ICE_GATHER_TIMEOUT_MS);
    signal.addEventListener("abort", onAbort, { once: true });
    peerConnection.addEventListener("icegatheringstatechange", onChange);
  });
}

function timedSignal(parentSignal: AbortSignal, timeoutMs: number): TimedSignal {
  const controller = new AbortController();
  const abort = (): void => controller.abort();
  parentSignal.addEventListener("abort", abort, { once: true });
  const timeout = window.setTimeout(abort, timeoutMs);
  return {
    signal: controller.signal,
    cleanup: (): void => {
      window.clearTimeout(timeout);
      parentSignal.removeEventListener("abort", abort);
    },
  };
}

/** Manage one device-scoped viewer without coupling it to parent rerenders. */
export function useCameraViewer(deviceId: string, videoRef: RefObject<HTMLVideoElement | null>): CameraViewerController {
  const [status, setStatus] = useState<PlayerStatus>("connecting");
  const [restartRequest, setRestartRequest] = useState(0);
  const reconnect = useCallback((): void => setRestartRequest((current) => current + 1), []);

  useEffect(() => {
    let active: ActiveViewer | null = null;
    let disposed = false;
    let hiddenTimer: number | null = null;
    let pageEnded = false;
    let retryCount = 0;
    let retryTimer: number | null = null;
    let sequence = 0;
    let viewerWanted = true;

    const isCurrent = (entry: ActiveViewer): boolean => (
      !disposed
      && viewerWanted
      && active === entry
      && sequence === entry.generation
      && !entry.controller.signal.aborted
    );
    const clearRetry = (): void => {
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      retryTimer = null;
    };
    const release = (entry: ActiveViewer | null): void => {
      if (entry === null) return;
      if (active === entry) active = null;
      entry.controller.abort();
      if (entry.heartbeatTimer !== null) window.clearInterval(entry.heartbeatTimer);
      entry.heartbeatController?.abort();
      if (entry.firstFrameTimer !== null) window.clearTimeout(entry.firstFrameTimer);
      if (entry.disconnectedTimer !== null) window.clearTimeout(entry.disconnectedTimer);
      if (entry.loadedDataHandler !== null && videoRef.current !== null) videoRef.current.removeEventListener("loadeddata", entry.loadedDataHandler);
      entry.peerConnection?.close();
      if (entry.sessionId !== null) void releaseWebRtc(deviceId, entry.sessionId).catch(() => undefined);
      void releaseViewer(deviceId, entry.viewerId).catch(() => undefined);
      if (videoRef.current) videoRef.current.srcObject = null;
    };
    const deactivate = (): void => {
      viewerWanted = false;
      sequence += 1;
      clearRetry();
      release(active);
    };
    const scheduleRetry = (): void => {
      if (disposed || !viewerWanted || retryTimer !== null) return;
      release(active);
      if (retryCount >= RETRY_DELAYS_MS.length) {
        setStatus("error");
        return;
      }
      setStatus("reconnecting");
      const delay = RETRY_DELAYS_MS[retryCount++] ?? RETRY_DELAYS_MS.at(-1)!;
      retryTimer = window.setTimeout(() => {
        retryTimer = null;
        void connect();
      }, delay);
    };
    const heartbeat = async (entry: ActiveViewer): Promise<void> => {
      if (!isCurrent(entry) || entry.heartbeatInFlight) return;
      entry.heartbeatInFlight = true;
      const heartbeatController = new AbortController();
      entry.heartbeatController = heartbeatController;
      const timedHeartbeat = timedSignal(heartbeatController.signal, HEARTBEAT_TIMEOUT_MS);
      try {
        await heartbeatViewer(deviceId, entry.viewerId, timedHeartbeat.signal);
      } catch {
        if (isCurrent(entry)) scheduleRetry();
      } finally {
        timedHeartbeat.cleanup();
        if (active === entry) {
          entry.heartbeatInFlight = false;
          entry.heartbeatController = null;
        }
      }
    };
    const connect = async (): Promise<void> => {
      if (disposed || !viewerWanted || active !== null) return;
      const entry: ActiveViewer = {
        controller: new AbortController(), disconnectedTimer: null, firstFrameReceived: false, firstFrameTimer: null,
        generation: ++sequence, heartbeatController: null, heartbeatInFlight: false, heartbeatTimer: null,
        loadedDataHandler: null, peerConnection: null, sessionId: null, viewerId: createViewerId(),
      };
      active = entry;
      setStatus(retryCount === 0 ? "connecting" : "reconnecting");
      try {
        await activateViewer(deviceId, entry.viewerId, entry.controller.signal);
        if (!isCurrent(entry)) {
          void releaseViewer(deviceId, entry.viewerId).catch(() => undefined);
          return;
        }
        entry.heartbeatTimer = window.setInterval(() => { void heartbeat(entry); }, HEARTBEAT_INTERVAL_MS);
        const peerConnection = new RTCPeerConnection({ bundlePolicy: "max-bundle" });
        entry.peerConnection = peerConnection;
        const stream = new MediaStream();
        peerConnection.ontrack = (event) => {
          if (!isCurrent(entry)) return;
          stream.addTrack(event.track);
          if (event.track.kind === "video" && videoRef.current) {
            videoRef.current.srcObject = stream;
            if (entry.loadedDataHandler === null) {
              const onLoadedData = (): void => {
                if (!isCurrent(entry) || videoRef.current?.srcObject !== stream) return;
                entry.firstFrameReceived = true;
                if (entry.firstFrameTimer !== null) window.clearTimeout(entry.firstFrameTimer);
                entry.firstFrameTimer = null;
                retryCount = 0;
                setStatus("live");
              };
              entry.loadedDataHandler = onLoadedData;
              videoRef.current.addEventListener("loadeddata", onLoadedData);
            }
            void videoRef.current.play().catch(() => undefined);
          }
        };
        peerConnection.onconnectionstatechange = () => {
          if (!isCurrent(entry)) return;
          if (peerConnection.connectionState === "failed") {
            scheduleRetry();
            return;
          }
          if (peerConnection.connectionState !== "disconnected") {
            if (entry.disconnectedTimer !== null) window.clearTimeout(entry.disconnectedTimer);
            entry.disconnectedTimer = null;
            return;
          }
          if (entry.disconnectedTimer === null) {
            entry.disconnectedTimer = window.setTimeout(() => {
              entry.disconnectedTimer = null;
              if (isCurrent(entry) && peerConnection.connectionState === "disconnected") scheduleRetry();
            }, DISCONNECTED_TIMEOUT_MS);
          }
        };
        peerConnection.addTransceiver("video", { direction: "recvonly" });
        peerConnection.addTransceiver("audio", { direction: "recvonly" });
        await peerConnection.setLocalDescription(await peerConnection.createOffer());
        const offer = await completeOffer(peerConnection, entry.controller.signal);
        if (!isCurrent(entry)) return;
        const whep = timedSignal(entry.controller.signal, WHEP_TIMEOUT_MS);
        let exchange: Awaited<ReturnType<typeof exchangeWebRtc>>;
        try {
          exchange = await exchangeWebRtc(deviceId, entry.viewerId, offer, whep.signal);
        } finally {
          whep.cleanup();
        }
        if (!isCurrent(entry)) {
          if (exchange.sessionId !== null) void releaseWebRtc(deviceId, exchange.sessionId).catch(() => undefined);
          return;
        }
        entry.sessionId = exchange.sessionId;
        await peerConnection.setRemoteDescription({ type: "answer", sdp: exchange.answer });
        if (!entry.firstFrameReceived) {
          entry.firstFrameTimer = window.setTimeout(() => {
            entry.firstFrameTimer = null;
            if (isCurrent(entry) && !entry.firstFrameReceived) scheduleRetry();
          }, FIRST_FRAME_TIMEOUT_MS);
        }
      } catch (error) {
        if (error instanceof WebRtcExchangeError && error.sessionId !== null) {
          void releaseWebRtc(deviceId, error.sessionId).catch(() => undefined);
        }
        if (isCurrent(entry)) scheduleRetry();
      }
    };
    const onPageHide = (): void => {
      pageEnded = true;
      deactivate();
    };
    const onPageShow = (event: PageTransitionEvent): void => {
      if (!event.persisted || disposed || !pageEnded) return;
      pageEnded = false;
      viewerWanted = true;
      retryCount = 0;
      void connect();
    };
    const onVisibilityChange = (): void => {
      if (document.hidden) {
        if (hiddenTimer === null) {
          hiddenTimer = window.setTimeout(() => {
            hiddenTimer = null;
            if (!pageEnded && document.hidden) {
              deactivate();
              setStatus("paused");
            }
          }, HIDDEN_VIEWER_TIMEOUT_MS);
        }
        return;
      }
      if (hiddenTimer !== null) window.clearTimeout(hiddenTimer);
      hiddenTimer = null;
      // Visibility alone is not an explicit media request. A hidden tab must
      // not recreate a WHEP consumer after its grace period has elapsed.
    };

    window.addEventListener("pagehide", onPageHide);
    window.addEventListener("pageshow", onPageShow);
    document.addEventListener("visibilitychange", onVisibilityChange);
    void connect();
    return () => {
      disposed = true;
      if (hiddenTimer !== null) window.clearTimeout(hiddenTimer);
      window.removeEventListener("pagehide", onPageHide);
      window.removeEventListener("pageshow", onPageShow);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      deactivate();
    };
  }, [deviceId, restartRequest, videoRef]);

  return { reconnect, status };
}
