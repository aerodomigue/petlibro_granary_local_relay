import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { dispense, getHome } from "../src/api/devices";
import { HomePage } from "../src/features/home/HomePage";

vi.mock("../src/api/devices", () => ({ dispense: vi.fn(), getHome: vi.fn() }));
vi.mock("../src/features/camera/CameraPlayer", () => ({ CameraPlayer: () => <section aria-label="Camera player" /> }));

afterEach(() => cleanup());

describe("HomePage", () => {
  it("renders a device card and device settings route", async () => {
    vi.mocked(getHome).mockResolvedValue({ status: { relay: { status: "running", uptime_seconds: 1 }, local_mqtt: { connected: true }, devices: { known: 1, local_online: 1, cloud_online: 1 } }, devices: [{ device_id: "device-a", product_id: "PLAF203", local_state: "LOCAL_ONLINE", last_seen_at: 1, rssi: -42, schedule: [], camera: { available: true, bridge_registered: true, go2rtc_reachable: true, online: true, media_consumers: 0 } }] });
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><HomePage /></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByRole("heading", { name: "PLAF203" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open feeder settings" })).toHaveAttribute("href", "/devices/device-a/settings");
  });

  it("starts at most one visible camera preview across a multi-device home", async () => {
    vi.mocked(getHome).mockResolvedValue({ status: { relay: { status: "running", uptime_seconds: 1 }, local_mqtt: { connected: true }, devices: { known: 2, local_online: 2, cloud_online: 2 } }, devices: ["device-a", "device-b"].map((device_id) => ({ device_id, product_id: "PLAF203", local_state: "LOCAL_ONLINE", last_seen_at: 1, rssi: -42, schedule: [], camera: { available: true, bridge_registered: true, go2rtc_reachable: true, online: true, media_consumers: 0 } })) });
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><HomePage /></MemoryRouter></QueryClientProvider>);

    await screen.findAllByRole("heading", { name: "PLAF203" });
    await waitFor(() => expect(document.querySelectorAll("[aria-label='Camera player']")).toHaveLength(1));
  });

  it("keeps manual dispense keyboard accessible and restores trigger focus on Escape", async () => {
    vi.mocked(getHome).mockResolvedValue({ status: { relay: { status: "running", uptime_seconds: 1 }, local_mqtt: { connected: true }, devices: { known: 1, local_online: 1, cloud_online: 1 } }, devices: [{ device_id: "device-a", product_id: "PLAF203", local_state: "LOCAL_ONLINE", last_seen_at: 1, rssi: -42, schedule: [], camera: { available: false, bridge_registered: false, go2rtc_reachable: false, online: false, media_consumers: 0 } }] });
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><HomePage /></MemoryRouter></QueryClientProvider>);
    const trigger = await screen.findByRole("button", { name: "Dispense now" });
    trigger.focus();
    fireEvent.click(trigger);
    expect(await screen.findByRole("dialog", { name: "Dispense now" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Dispense now" })).not.toBeInTheDocument();
    expect(document.activeElement).toBe(trigger);
  });

  it("does not close the dispense dialog with Escape while feeder acknowledgement is pending", async () => {
    let resolveDispense: () => void = () => undefined;
    vi.mocked(dispense).mockReturnValue(new Promise<void>((resolve) => { resolveDispense = resolve; }));
    vi.mocked(getHome).mockResolvedValue({ status: { relay: { status: "running", uptime_seconds: 1 }, local_mqtt: { connected: true }, devices: { known: 1, local_online: 1, cloud_online: 1 } }, devices: [{ device_id: "device-a", product_id: "PLAF203", local_state: "LOCAL_ONLINE", last_seen_at: 1, rssi: -42, schedule: [], camera: { available: false, bridge_registered: false, go2rtc_reachable: false, online: false, media_consumers: 0 } }] });
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><HomePage /></MemoryRouter></QueryClientProvider>);
    fireEvent.click(await screen.findByRole("button", { name: "Dispense now" }));
    fireEvent.click(await screen.findByRole("button", { name: "Dispense" }));
    expect(await screen.findByText("Dispensing…", { exact: true })).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.getByRole("dialog", { name: "Dispense now" })).toBeInTheDocument();
    resolveDispense();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Dispense now" })).not.toBeInTheDocument());
  });
});
