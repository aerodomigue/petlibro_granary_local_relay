import { request } from "./client";

export interface WebRtcExchange {
  answer: string;
  sessionId: string | null;
}

export class WebRtcExchangeError extends Error {
  public constructor(message: string, public readonly sessionId: string | null) {
    super(message);
    this.name = "WebRtcExchangeError";
  }
}

function viewerPath(deviceId: string, viewerId: string): string {
  return `/api/devices/${encodeURIComponent(deviceId)}/camera/viewers/${encodeURIComponent(viewerId)}`;
}

export function activateViewer(deviceId: string, viewerId: string, signal: AbortSignal): Promise<void> {
  return request<void>(viewerPath(deviceId, viewerId), { method: "POST", signal });
}

export function heartbeatViewer(deviceId: string, viewerId: string, signal: AbortSignal): Promise<void> {
  return request<void>(viewerPath(deviceId, viewerId), { method: "PUT", signal });
}

export function releaseViewer(deviceId: string, viewerId: string): Promise<void> {
  return request<void>(viewerPath(deviceId, viewerId), { method: "DELETE", keepalive: true });
}

export async function exchangeWebRtc(deviceId: string, viewerId: string, offer: string, signal: AbortSignal): Promise<WebRtcExchange> {
  const response = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/camera/webrtc`, {
    method: "POST",
    signal,
    headers: { "Content-Type": "application/sdp", "X-Relay-Viewer-ID": viewerId },
    body: offer,
  });
  if (!response.ok) throw new Error(`Camera negotiation failed: HTTP ${response.status}`);
  const sessionId = response.headers.get("X-Relay-WebRTC-Session");
  try {
    return { answer: await response.text(), sessionId };
  } catch (error) {
    const message = error instanceof Error ? error.message : "Camera negotiation response could not be read";
    throw new WebRtcExchangeError(message, sessionId);
  }
}

export function releaseWebRtc(deviceId: string, sessionId: string): Promise<void> {
  return request<void>(`/api/devices/${encodeURIComponent(deviceId)}/camera/webrtc/${encodeURIComponent(sessionId)}`, { method: "DELETE", keepalive: true });
}
