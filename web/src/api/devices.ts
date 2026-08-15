import { request } from "./client";
import type { HomeResponse } from "../types/api";

export function getHome(signal?: AbortSignal): Promise<HomeResponse> {
  return request<HomeResponse>("/api/home", { signal });
}
