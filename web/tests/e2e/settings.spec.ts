import { expect, test, type Page } from "./fixtures";

const DEVICE_ID = "device-a";

function dailyResponse(activity: object[] = []): object {
  return {
    activity,
    camera: { available: false, bridge_registered: false, go2rtc_reachable: false, media_consumers: 0, online: false },
    controls: { soundSwitch: { cloud_sync_confirmed: true, control: "soundSwitch", device_ack_confirmed: true, device_online: true, pending: false, required_state_available: true, writable: true } },
    device: { device_id: DEVICE_ID, last_seen_at: 1, local_state: "LOCAL_ONLINE", product_id: "PLAF203", rssi: -42, schedule: [] },
    state: { desired: [{ key: "soundSwitch", value: false }, { key: "soundAgingType", value: 1 }], local_confirmed: [], schedule_plans: [] },
  };
}

async function mockApi(page: Page): Promise<void> {
  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = (body: object, status = 200): Promise<void> => route.fulfill({ body: JSON.stringify(body), contentType: "application/json", status });
    if (path === `/api/devices/${DEVICE_ID}/daily`) return json(dailyResponse());
    if (path === `/api/devices/${DEVICE_ID}/controls/sound` && request.method() === "PATCH") return json({ success: true });
    if (path === `/api/devices/${DEVICE_ID}/advanced`) return json({ camera: {}, connectivity: {}, device: { cloud_state: "ONLINE", device_id: DEVICE_ID, firmware: "V3", ip: "10.0.0.2", local_state: "LOCAL_ONLINE", mac: "00", product_id: "PLAF203", rssi: -42 }, logs: [], relay: {}, state_summary: {} });
    return json({ detail: "Unexpected API request" }, 404);
  });
}

test("global Advanced preference persists and gates the device route", async ({ page }) => {
  await mockApi(page);
  await page.goto("/settings");
  const toggle = page.getByRole("switch", { name: /^Advanced diagnostics/ });
  await expect(toggle).not.toBeChecked();
  await toggle.check();
  await page.reload();
  await expect(toggle).toBeChecked();
  await page.goto(`/devices/${DEVICE_ID}/advanced`);
  await expect(page.getByRole("heading", { name: "Advanced diagnostics" })).toBeVisible();
  await page.goto("/settings");
  await toggle.uncheck();
  await page.goto(`/devices/${DEVICE_ID}/advanced`);
  await expect(page.getByRole("heading", { name: "Advanced mode is off" })).toBeVisible();
});

test("device settings preserve an edited field through polling and use a typed save", async ({ page }) => {
  await mockApi(page);
  await page.goto(`/devices/${DEVICE_ID}/settings`);
  const sound = page.getByRole("switch", { name: "Enable device sound" });
  await sound.check();
  await sound.focus();
  await page.waitForTimeout(3_250);
  await expect(sound).toBeChecked();
  expect(await page.evaluate(() => document.activeElement?.id)).toBe("setting-soundSwitch");
  await page.getByRole("button", { name: "Save speaker settings" }).click();
  await expect(page.getByText("Saved. Feeder confirmed the change.")).toBeVisible();
});

test("Activity is user-facing and has no horizontal overflow", async ({ page }) => {
  await mockApi(page);
  await page.goto(`/devices/${DEVICE_ID}/activity`);
  await expect(page.getByText("No activity yet")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth === document.documentElement.clientWidth)).toBe(true);
});
