import { request } from "./client";
import type { CameraAvailability, HomeResponse } from "../types/api";

export function getHome(signal?: AbortSignal): Promise<HomeResponse> {
  return request<HomeResponse>("/api/home", { signal });
}

export function dispense(deviceId: string, grainNum: number): Promise<void> {
  return request<void>(`/api/devices/${encodeURIComponent(deviceId)}/dispense`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ grainNum }),
  });
}

export function getCameraStatus(deviceId: string, signal?: AbortSignal): Promise<CameraAvailability> {
  return request<CameraAvailability>(`/api/devices/${encodeURIComponent(deviceId)}/camera`, { signal });
}
