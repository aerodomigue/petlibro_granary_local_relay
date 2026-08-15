import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createSchedule, deleteSchedule, getSchedules, updateSchedule } from "../src/api/schedules";
import { queryKeys } from "../src/api/queryKeys";
import { SchedulePage } from "../src/features/schedules/SchedulePage";
import type { ScheduleSnapshot } from "../src/types/api";

vi.mock("../src/api/schedules", () => ({ createSchedule: vi.fn(), deleteSchedule: vi.fn(), getSchedules: vi.fn(), updateSchedule: vi.fn() }));

const DEVICE_ID = "device-a";
const SCHEDULE: ScheduleSnapshot = { plan: { planId: -1, executionTime: "07:30", grainNum: 3, enableAudio: true, audioTimes: 1, repeatDay: [1, 2, 3, 4, 5] }, source: "local", updatedAt: 1 };
const scheduleData = (schedules: ScheduleSnapshot[]) => ({ device: { camera: { available: false, bridge_registered: false, go2rtc_reachable: false, media_consumers: 0, online: false }, device_id: DEVICE_ID, last_seen_at: 1, local_state: "LOCAL_ONLINE", product_id: "PLAF203", rssi: -42, schedule: [] }, schedules });
const SUCCESS = { cloud_sync_behavior: "unknown" as const, control: "schedule:update", device_ack: true, device_id: DEVICE_ID, success: true, value: { plans: [] } };

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

function renderSchedule(): QueryClient {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><MemoryRouter initialEntries={[`/devices/${DEVICE_ID}/schedule`]}><Routes><Route element={<SchedulePage />} path="/devices/:deviceId/schedule" /></Routes></MemoryRouter></QueryClientProvider>);
  return queryClient;
}

describe("SchedulePage", () => {
  it("renders schedules and preserves an editor draft and focus during a query refresh", async () => {
    vi.mocked(getSchedules).mockResolvedValue(scheduleData([SCHEDULE]));
    const queryClient = renderSchedule();
    await screen.findByText("07:30");
    fireEvent.click(screen.getByRole("button", { name: "Edit scheduled meal at 07:30" }));
    const portions = await screen.findByLabelText("Portions");
    fireEvent.change(portions, { target: { value: "30" } });
    portions.focus();
    queryClient.setQueryData(queryKeys.schedule(DEVICE_ID), scheduleData([{ ...SCHEDULE, plan: { ...SCHEDULE.plan, grainNum: 1 } }]));
    await waitFor(() => expect(screen.getByLabelText("Portions")).toHaveValue(30));
    expect(document.activeElement).toBe(portions);
  });

  it("creates the strict API payload only once and closes after feeder-confirmed success", async () => {
    vi.mocked(getSchedules).mockResolvedValue(scheduleData([]));
    vi.mocked(createSchedule).mockResolvedValue({ ...SUCCESS, control: "schedule:create" });
    renderSchedule();
    fireEvent.click(await screen.findByRole("button", { name: "+ Add a meal" }));
    fireEvent.change(screen.getByLabelText("Portions"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save meal" }));
    await waitFor(() => expect(createSchedule).toHaveBeenCalledWith(DEVICE_ID, { executionTime: "07:30", grainNum: 2, enableAudio: false, audioTimes: 1, repeatDay: [1, 2, 3, 4, 5, 6, 7] }));
    expect(createSchedule).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add a meal" })).not.toBeInTheDocument());
  });

  it("keeps the draft open after an ACK/API error", async () => {
    vi.mocked(getSchedules).mockResolvedValue(scheduleData([]));
    vi.mocked(createSchedule).mockRejectedValue(new Error("Device acknowledgement timeout"));
    renderSchedule();
    fireEvent.click(await screen.findByRole("button", { name: "+ Add a meal" }));
    fireEvent.change(screen.getByLabelText("Portions"), { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: "Save meal" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Device acknowledgement timeout");
    expect(screen.getByLabelText("Portions")).toHaveValue(30);
  });

  it("keeps an edit draft open after an update error", async () => {
    vi.mocked(getSchedules).mockResolvedValue(scheduleData([SCHEDULE]));
    vi.mocked(updateSchedule).mockRejectedValue(new Error("Device acknowledgement timeout"));
    renderSchedule();
    fireEvent.click(await screen.findByRole("button", { name: "Edit scheduled meal at 07:30" }));
    fireEvent.change(screen.getByLabelText("Time"), { target: { value: "12:34" } });
    fireEvent.click(screen.getByRole("button", { name: "Save meal" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Device acknowledgement timeout");
    expect(screen.getByLabelText("Time")).toHaveValue("12:34");
  });

  it("rejects a second Save without publishing a second feeder command", async () => {
    let resolveCreate: () => void = () => undefined;
    vi.mocked(getSchedules).mockResolvedValue(scheduleData([]));
    vi.mocked(createSchedule).mockReturnValue(new Promise((resolve) => { resolveCreate = () => resolve({ ...SUCCESS, control: "schedule:create" }); }));
    renderSchedule();
    fireEvent.click(await screen.findByRole("button", { name: "+ Add a meal" }));
    const save = screen.getByRole("button", { name: "Save meal" });
    fireEvent.click(save);
    await waitFor(() => expect(createSchedule).toHaveBeenCalledTimes(1));
    fireEvent.click(save);
    expect(createSchedule).toHaveBeenCalledTimes(1);
    resolveCreate();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add a meal" })).not.toBeInTheDocument());
  });

  it("does not optimistically disable a schedule when its feeder mutation fails", async () => {
    vi.mocked(getSchedules).mockResolvedValue(scheduleData([SCHEDULE]));
    vi.mocked(updateSchedule).mockRejectedValue(new Error("Device is offline"));
    renderSchedule();
    await screen.findByText("✓ Active");
    fireEvent.click(screen.getByRole("button", { name: "Disable scheduled meal at 07:30" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Device is offline");
    expect(screen.getByText("✓ Active")).toBeInTheDocument();
    expect(updateSchedule).toHaveBeenCalledWith(DEVICE_ID, -1, { repeatDay: [] });
  });

  it("asks for repeat days before enabling a disabled plan whose prior days are unknown", async () => {
    vi.mocked(getSchedules).mockResolvedValue(scheduleData([{ ...SCHEDULE, plan: { ...SCHEDULE.plan, repeatDay: [] } }]));
    renderSchedule();
    fireEvent.click(await screen.findByRole("button", { name: "Enable scheduled meal at 07:30" }));
    expect(await screen.findByRole("dialog", { name: "Edit scheduled meal" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Choose at least one day");
    expect(updateSchedule).not.toHaveBeenCalled();
  });

  it("keeps schedule actions unavailable while the feeder is offline", async () => {
    vi.mocked(getSchedules).mockResolvedValue({ ...scheduleData([SCHEDULE]), device: { ...scheduleData([]).device, local_state: "OFFLINE" } });
    renderSchedule();
    expect(await screen.findByText("Feeder offline. Schedule changes are unavailable until it reconnects.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "+ Add a meal" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Edit scheduled meal at 07:30" })).toBeDisabled();
  });

  it("keeps a plan and its delete dialog on mutation failure", async () => {
    vi.mocked(getSchedules).mockResolvedValue(scheduleData([SCHEDULE]));
    vi.mocked(deleteSchedule).mockRejectedValue(new Error("Device acknowledgement timeout"));
    renderSchedule();
    await screen.findByText("07:30");
    fireEvent.click(screen.getByRole("button", { name: "Delete scheduled meal at 07:30" }));
    fireEvent.click(await within(screen.getByRole("dialog", { name: "Delete scheduled meal?" })).findByRole("button", { name: /^Delete$/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Device acknowledgement timeout");
    expect(screen.getByText("07:30")).toBeInTheDocument();
  });

  it("keeps the editor accessible with Escape and returns focus to its trigger", async () => {
    vi.mocked(getSchedules).mockResolvedValue(scheduleData([]));
    renderSchedule();
    const trigger = await screen.findByRole("button", { name: "+ Add a meal" });
    trigger.focus();
    fireEvent.click(trigger);
    expect(await screen.findByRole("dialog", { name: "Add a meal" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Add a meal" })).not.toBeInTheDocument();
    expect(document.activeElement).toBe(trigger);
  });
});
