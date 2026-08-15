import { expect, test, type Page } from "./fixtures";

const DEVICE_ID = "device-a";

interface Plan {
  audioTimes: number;
  enableAudio: boolean;
  executionTime: string;
  grainNum: number;
  planId: number;
  repeatDay: number[];
}

function scheduleResponse(plans: Plan[]): object {
  return {
    activity: [],
    camera: { available: false, bridge_registered: false, go2rtc_reachable: false, media_consumers: 0, online: false },
    controls: {},
    device: { device_id: DEVICE_ID, last_seen_at: 1, local_state: "LOCAL_ONLINE", product_id: "PLAF203", rssi: -42, schedule: [] },
    state: { desired: [], local_confirmed: [], schedule_plans: plans.map((plan) => ({ plan, source: "local", updated_at: 1 })) },
  };
}

interface ScheduleCalls {
  creates: Plan[];
  deletes: number[];
  updates: Array<{ planId: number; values: Partial<Plan> }>;
}

async function mockScheduleApi(page: Page, options: { delayUpdate?: boolean; fail?: "create" | "delete" | "update" } = {}): Promise<ScheduleCalls> {
  const calls: ScheduleCalls = { creates: [], deletes: [], updates: [] };
  let plans: Plan[] = [{ audioTimes: 1, enableAudio: true, executionTime: "07:30", grainNum: 3, planId: -1, repeatDay: [1, 2, 3, 4, 5] }];
  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const json = (body: object, status = 200): Promise<void> => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === `/api/devices/${DEVICE_ID}/daily`) return json(scheduleResponse(plans));
    if (url.pathname === `/api/devices/${DEVICE_ID}/schedule` && request.method() === "POST") {
      if (options.fail === "create") return json({ detail: "Device acknowledgement timeout" }, 504);
      const values = JSON.parse(request.postData() ?? "{}") as Omit<Plan, "planId">;
      const plan: Plan = { ...values, planId: -2 };
      calls.creates.push(plan);
      plans = [...plans, plan];
      return json({ success: true, device_ack: true, device_id: DEVICE_ID, control: "schedule:create", cloud_sync_behavior: "unknown", value: { plans } });
    }
    const match = url.pathname.match(new RegExp(`/api/devices/${DEVICE_ID}/schedule/(-?\\d+)$`));
    if (match && request.method() === "PATCH") {
      if (options.delayUpdate) await new Promise((resolve) => setTimeout(resolve, 500));
      if (options.fail === "update") return json({ detail: "Device is offline" }, 409);
      const planId = Number(match[1]);
      const values = JSON.parse(request.postData() ?? "{}") as Partial<Plan>;
      calls.updates.push({ planId, values });
      plans = plans.map((plan) => plan.planId === planId ? { ...plan, ...values } : plan);
      return json({ success: true, device_ack: true, device_id: DEVICE_ID, control: "schedule:update", cloud_sync_behavior: "unknown", value: { plans } });
    }
    if (match && request.method() === "DELETE") {
      if (options.fail === "delete") return json({ detail: "Device acknowledgement timeout" }, 504);
      const planId = Number(match[1]);
      calls.deletes.push(planId);
      plans = plans.filter((plan) => plan.planId !== planId);
      return json({ success: true, device_ack: true, device_id: DEVICE_ID, control: "schedule:delete", cloud_sync_behavior: "unknown", value: { plans } });
    }
    return json({ detail: "Unexpected API request" }, 404);
  });
  return calls;
}

test("Schedule is usable without horizontal overflow at every supported viewport", async ({ page }) => {
  await mockScheduleApi(page);
  await page.goto(`/devices/${DEVICE_ID}/schedule`);
  await expect(page.getByRole("heading", { name: "Schedule" })).toBeVisible();
  await expect(page.getByText("07:30")).toBeVisible();
  await expect(page.getByRole("button", { name: "Edit scheduled meal at 07:30" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth === document.documentElement.clientWidth)).toBe(true);
});

test("Schedule create, edit, disable and delete use feeder-confirmed APIs", async ({ page }) => {
  const calls = await mockScheduleApi(page);
  await page.goto(`/devices/${DEVICE_ID}/schedule`);
  await page.getByRole("button", { name: "+ Add a meal" }).click();
  await page.getByLabel("Time").fill("18:00");
  await page.getByLabel("Portions").fill("2");
  await page.getByRole("button", { name: "Save meal" }).click();
  await expect.poll(() => calls.creates).toHaveLength(1);
  expect(calls.creates[0]).toMatchObject({ executionTime: "18:00", grainNum: 2, repeatDay: [1, 2, 3, 4, 5, 6, 7] });

  await page.getByRole("button", { name: "Edit scheduled meal at 18:00" }).click();
  await page.getByLabel("Time").fill("12:34");
  await page.getByRole("button", { name: "Save meal" }).click();
  await expect.poll(() => calls.updates.some((call) => call.values.executionTime === "12:34")).toBe(true);
  await expect(page.getByText("12:34")).toBeVisible();

  await page.getByRole("button", { name: "Disable scheduled meal at 12:34" }).click();
  await expect.poll(() => calls.updates.some((call) => Array.isArray(call.values.repeatDay) && call.values.repeatDay.length === 0)).toBe(true);
  await expect(page.getByText("○ Disabled")).toBeVisible();

  await page.getByRole("button", { name: "Delete scheduled meal at 12:34" }).click();
  await page.getByRole("dialog", { name: "Delete scheduled meal?" }).getByRole("button", { name: /^Delete$/ }).click();
  await expect.poll(() => calls.deletes).toContain(-2);
});

test("Schedule editor retains its draft and focus across a real polling refetch", async ({ page }) => {
  await mockScheduleApi(page);
  await page.goto(`/devices/${DEVICE_ID}/schedule`);
  await page.getByRole("button", { name: "Edit scheduled meal at 07:30" }).click();
  const portions = page.getByLabel("Portions");
  await portions.fill("30");
  await portions.focus();
  await page.waitForTimeout(3_250);
  await expect(portions).toHaveValue("30");
  expect(await page.evaluate(() => document.activeElement?.id)).toBe("schedule-portions");
});

test("Schedule mutation errors preserve the user's draft", async ({ page }) => {
  await mockScheduleApi(page, { fail: "create" });
  await page.goto(`/devices/${DEVICE_ID}/schedule`);
  await page.getByRole("button", { name: "+ Add a meal" }).click();
  await page.getByLabel("Portions").fill("30");
  await page.getByRole("button", { name: "Save meal" }).click();
  await expect(page.getByRole("alert")).toContainText("Device acknowledgement timeout");
  await expect(page.getByLabel("Portions")).toHaveValue("30");
});
