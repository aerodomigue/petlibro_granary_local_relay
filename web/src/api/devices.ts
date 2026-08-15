import { request } from "./client";
import type { DailyDeviceResponse, HomeResponse } from "../types/api";

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

export function getDailyDevice(deviceId: string, signal?: AbortSignal): Promise<DailyDeviceResponse> {
  return request<DailyDeviceResponse>(`/api/devices/${encodeURIComponent(deviceId)}/daily`, { signal });
}
