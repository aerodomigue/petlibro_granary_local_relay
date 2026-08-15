import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getDailyDevice } from "../src/api/deviceDetails";
import { OverviewPage } from "../src/features/overview/OverviewPage";
import { PreferencesProvider } from "../src/preferences/PreferencesContext";

vi.mock("../src/api/deviceDetails", () => ({ getDailyDevice: vi.fn() }));

const DEVICE_ID = "device-a";
const DAILY = {
  activity: [],
  camera: { available: false, bridge_registered: false, go2rtc_reachable: false, media_consumers: 0, online: false },
  controls: {},
  device: {
    camera: { available: false, bridge_registered: false, go2rtc_reachable: false, media_consumers: 0, online: false },
    device_id: DEVICE_ID,
    last_seen_at: Math.floor(Date.now() / 1_000),
    local_state: "LOCAL_ONLINE",
    product_id: "PLAF203",
    rssi: -42,
    schedule: [{ execution_time: "07:30", grain_num: 3, repeat_day: [1, 2, 3, 4, 5, 6, 7] }],
  },
  state: { desired: [], local_confirmed: [], schedule_plans: [] },
};

afterEach(() => { cleanup(); vi.resetAllMocks(); });

function renderOverview(): void {
  render(<PreferencesProvider><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={[`/devices/${DEVICE_ID}/overview`]}><Routes><Route element={<OverviewPage />} path="/devices/:deviceId/overview" /></Routes></MemoryRouter></QueryClientProvider></PreferencesProvider>);
}

describe("OverviewPage", () => {
  it("uses the safe daily projection and provides ordinary feeder actions", async () => {
    vi.mocked(getDailyDevice).mockResolvedValue(DAILY);
    renderOverview();

    expect(await screen.findByRole("heading", { name: "PLAF203" })).toBeInTheDocument();
    expect(screen.getByText("Wi-Fi excellent")).toBeInTheDocument();
    expect(screen.getByText(/07:30.*3 portions/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View camera status" })).toHaveAttribute("href", `/devices/${DEVICE_ID}/camera`);
    expect(screen.queryByText("device-a")).not.toBeInTheDocument();
  });

  it("keeps the dispense dialog local to the Overview route", async () => {
    vi.mocked(getDailyDevice).mockResolvedValue(DAILY);
    renderOverview();

    fireEvent.click(await screen.findByRole("button", { name: "Dispense now" }));
    expect(screen.getByRole("dialog", { name: "Dispense now" })).toBeInTheDocument();
  });
});
