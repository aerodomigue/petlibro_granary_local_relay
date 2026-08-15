import { request } from "./client";
import { parseCameraAvailability, parseHomeResponse } from "./contracts";
import type { CameraAvailability, HomeResponse } from "../types/api";

export async function getHome(signal?: AbortSignal): Promise<HomeResponse> {
  return parseHomeResponse(await request<unknown>("/api/home", { signal }));
}

export function dispense(deviceId: string, grainNum: number): Promise<void> {
  return request<void>(`/api/devices/${encodeURIComponent(deviceId)}/dispense`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ grainNum }),
  });
}

export async function getCameraStatus(deviceId: string, signal?: AbortSignal): Promise<CameraAvailability> {
  return parseCameraAvailability(await request<unknown>(`/api/devices/${encodeURIComponent(deviceId)}/camera`, { signal }));
}
