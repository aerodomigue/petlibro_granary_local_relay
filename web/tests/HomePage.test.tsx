import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { getHome } from "../src/api/devices";
import { HomePage } from "../src/features/home/HomePage";

vi.mock("../src/api/devices", () => ({ getHome: vi.fn() }));

describe("HomePage", () => {
  it("renders a device card and device settings route", async () => {
    vi.mocked(getHome).mockResolvedValue({ status: { relay: { status: "running", uptime_seconds: 1 }, local_mqtt: { connected: true }, devices: { known: 1, local_online: 1, cloud_online: 1 } }, devices: [{ device_id: "device-a", product_id: "PLAF203", local_state: "LOCAL_ONLINE", last_seen_at: 1, rssi: -42, schedule: [], camera: { bridge_registered: true, go2rtc_reachable: true, online: true, media_consumers: 0 } }] });
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><HomePage /></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByRole("heading", { name: "PLAF203" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open feeder settings" })).toHaveAttribute("href", "/devices/device-a/overview");
  });
});
