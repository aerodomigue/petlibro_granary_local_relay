import { request } from "./client";

export interface WebRtcExchange {
  answer: string;
  sessionId: string | null;
}

function viewerPath(deviceId: string, viewerId: string): string {
  return `/api/devices/${encodeURIComponent(deviceId)}/camera/viewers/${encodeURIComponent(viewerId)}`;
}

export function activateViewer(deviceId: string, viewerId: string, signal: AbortSignal): Promise<void> {
  return request<void>(viewerPath(deviceId, viewerId), { method: "POST", signal });
}

export function heartbeatViewer(deviceId: string, viewerId: string): Promise<void> {
  return request<void>(viewerPath(deviceId, viewerId), { method: "PUT", keepalive: true });
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
  return { answer: await response.text(), sessionId: response.headers.get("X-Relay-WebRTC-Session") };
}

export function releaseWebRtc(deviceId: string, sessionId: string): Promise<void> {
  return request<void>(`/api/devices/${encodeURIComponent(deviceId)}/camera/webrtc/${encodeURIComponent(sessionId)}`, { method: "DELETE", keepalive: true });
}
