import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getCameraStatus } from "../src/api/devices";
import { CameraPage } from "../src/features/camera/CameraPage";

vi.mock("../src/api/devices", () => ({ getCameraStatus: vi.fn() }));
vi.mock("../src/features/camera/CameraPlayer", () => ({ CameraPlayer: () => <section aria-label="Camera player" /> }));

const DEVICE_ID = "device-a";
const ONLINE_CAMERA = { available: true, bridge_registered: true, go2rtc_reachable: true, media_consumers: 1, online: true };
const TRANSIENT_CAMERA_FAILURE = { ...ONLINE_CAMERA, bridge_registered: false, go2rtc_reachable: false, reason: "Reconnecting" };

afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe("CameraPage", () => {
  it("does not unmount a live player when one availability poll is temporarily degraded", async () => {
    vi.mocked(getCameraStatus).mockResolvedValue(ONLINE_CAMERA);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[`/devices/${DEVICE_ID}/camera`]}><Routes><Route element={<CameraPage />} path="/devices/:deviceId/camera" /></Routes></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByRole("region", { name: "Camera player" })).toBeInTheDocument();
    client.setQueryData(["device", DEVICE_ID, "camera"], TRANSIENT_CAMERA_FAILURE);
    await waitFor(() => expect(screen.getByRole("region", { name: "Camera player" })).toBeInTheDocument());
    expect(screen.queryByText("Camera unavailable")).not.toBeInTheDocument();
  });
});
