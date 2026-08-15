import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { JSX } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getAdvancedDevice, getDailyDevice, updateControlGroup } from "../src/api/deviceDetails";
import { ActivityPage } from "../src/features/activity/ActivityPage";
import { AdvancedPage } from "../src/features/advanced/AdvancedPage";
import { DeviceSettingsPage } from "../src/features/settings/DeviceSettingsPage";
import { SettingsPage } from "../src/features/settings/SettingsPage";
import { PreferencesProvider } from "../src/preferences/PreferencesContext";

vi.mock("../src/api/deviceDetails", () => ({ getAdvancedDevice: vi.fn(), getDailyDevice: vi.fn(), updateControlGroup: vi.fn() }));

const DEVICE_ID = "device-a";
const CAPABILITY = { cloud_sync_confirmed: true, control: "soundSwitch", device_ack_confirmed: true, device_online: true, pending: false, required_state_available: true, writable: true };
const DAILY = {
  activity: [],
  camera: { available: false, bridge_registered: false, go2rtc_reachable: false, media_consumers: 0, online: false },
  controls: { soundSwitch: CAPABILITY },
  device: { camera: { available: false, bridge_registered: false, go2rtc_reachable: false, media_consumers: 0, online: false }, device_id: DEVICE_ID, last_seen_at: 1, local_state: "LOCAL_ONLINE", product_id: "PLAF203", rssi: -42, schedule: [] },
  state: { desired: [{ key: "soundSwitch", value: false }, { key: "soundAgingType", value: 1 }], local_confirmed: [], schedule_plans: [] },
};
const ADVANCED = { camera: { available: true, uid_learned: true }, connectivity: { upstream_state: "ONLINE" }, device: { cloud_state: "ONLINE", device_id: DEVICE_ID, firmware: "V3", ip: "10.0.0.2", local_state: "LOCAL_ONLINE", mac: "00:00:00:00:00:00", product_id: "PLAF203", rssi: -42 }, logs: [], relay: { local_responder: false }, state_summary: { desired_values: 2 } };

afterEach(() => { cleanup(); vi.resetAllMocks(); });

function renderPage(path: string, element: JSX.Element): QueryClient {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<PreferencesProvider><QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><Routes><Route element={element} path="/settings" /><Route element={element} path="/devices/:deviceId/settings" /><Route element={element} path="/devices/:deviceId/activity" /><Route element={element} path="/devices/:deviceId/advanced" /></Routes></MemoryRouter></QueryClientProvider></PreferencesProvider>);
  return client;
}

describe("Settings, Advanced and Activity", () => {
  it("persists Advanced mode and exposes the switch accessibly", () => {
    renderPage("/settings", <SettingsPage />);
    const toggle = screen.getByRole("switch", { name: /^Advanced diagnostics/ });
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    expect(toggle).toBeChecked();
    expect(toggle).toBeChecked();
  });

  it("keeps a dirty device-setting draft and focus across a query refresh", async () => {
    vi.mocked(getDailyDevice).mockResolvedValue(DAILY);
    const client = renderPage(`/devices/${DEVICE_ID}/settings`, <DeviceSettingsPage />);
    const sound = await screen.findByRole("switch", { name: "Enable device sound" });
    fireEvent.click(sound);
    sound.focus();
    client.setQueryData(["device", DEVICE_ID, "settings"], { ...DAILY, state: { ...DAILY.state, desired: [{ key: "soundSwitch", value: false }, { key: "soundAgingType", value: 2 }] } });
    await waitFor(() => expect(sound).toBeChecked());
    expect(document.activeElement).toBe(sound);
  });

  it("refreshes clean device-setting fields from the server snapshot", async () => {
    vi.mocked(getDailyDevice).mockResolvedValue(DAILY);
    const client = renderPage(`/devices/${DEVICE_ID}/settings`, <DeviceSettingsPage />);
    const sound = await screen.findByRole("switch", { name: "Enable device sound" });
    expect(sound).not.toBeChecked();
    client.setQueryData(["device", DEVICE_ID, "settings"], { ...DAILY, state: { ...DAILY.state, desired: [{ key: "soundSwitch", value: true }, { key: "soundAgingType", value: 1 }] } });
    await waitFor(() => expect(sound).toBeChecked());
  });

  it("waits for a successful typed device-settings request before confirming save", async () => {
    vi.mocked(getDailyDevice).mockResolvedValue(DAILY);
    vi.mocked(updateControlGroup).mockResolvedValue(undefined);
    renderPage(`/devices/${DEVICE_ID}/settings`, <DeviceSettingsPage />);
    fireEvent.click(await screen.findByRole("switch", { name: "Enable device sound" }));
    fireEvent.click(screen.getByRole("button", { name: "Save speaker settings" }));
    await waitFor(() => expect(updateControlGroup).toHaveBeenCalledWith(DEVICE_ID, "sound", { soundSwitch: true }));
    expect(await screen.findByText("Saved. Feeder confirmed the change.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save speaker settings" })).toBeDisabled();
  });

  it("keeps a failed device-settings draft for retry", async () => {
    vi.mocked(getDailyDevice).mockResolvedValue(DAILY);
    vi.mocked(updateControlGroup).mockRejectedValue(new Error("Device acknowledgement timeout"));
    renderPage(`/devices/${DEVICE_ID}/settings`, <DeviceSettingsPage />);
    const sound = await screen.findByRole("switch", { name: "Enable device sound" });
    fireEvent.click(sound);
    fireEvent.click(screen.getByRole("button", { name: "Save speaker settings" }));
    expect(await screen.findByText(/Unable to save: Device acknowledgement timeout/)).toBeInTheDocument();
    expect(sound).toBeChecked();
    expect(screen.getByRole("button", { name: "Save speaker settings" })).toBeEnabled();
  });

  it("blocks Advanced before querying when the preference is off", () => {
    renderPage(`/devices/${DEVICE_ID}/advanced`, <AdvancedPage />);
    expect(getAdvancedDevice).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "Advanced mode is off" })).toBeInTheDocument();
  });

  it("renders only feeder activity and keeps an empty state honest", async () => {
    vi.mocked(getDailyDevice).mockResolvedValue({ ...DAILY, activity: [{ kind: "feeder_dispensing", timestamp: 1_700_000_000 }, { kind: "feeder_error", timestamp: 2 }] });
    renderPage(`/devices/${DEVICE_ID}/activity`, <ActivityPage />);
    expect(await screen.findByText("Dispensing activity")).toBeInTheDocument();
    expect(screen.getByText("Feeder needs attention")).toBeInTheDocument();
    expect(document.querySelectorAll("time")[1]).toHaveAttribute("dateTime", "2023-11-14T22:13:20.000Z");
  });
});
