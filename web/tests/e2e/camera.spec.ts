import { expect, test, type Page, type TestInfo } from "@playwright/test";

const DEVICE_ID = "device-a";

async function mockMediaBrowser(page: Page): Promise<void> {
  await page.addInitScript(() => {
    window.addEventListener("unhandledrejection", (event) => {
      console.error(`Unhandled rejection: ${String(event.reason)}`);
    });
    class FakeMediaStream { public addTrack(): void {} }
    class FakePeerConnection {
      public iceGatheringState = "complete";
      public localDescription = { type: "offer", sdp: "offer" };
      public connectionState = "connected";
      public ontrack: ((event: { track: { kind: string } }) => void) | null = null;
      public onconnectionstatechange: (() => void) | null = null;
      public addEventListener(): void {}
      public removeEventListener(): void {}
      public addTransceiver(): void {}
      public async createOffer(): Promise<RTCSessionDescriptionInit> { return { type: "offer", sdp: "offer" }; }
      public async setLocalDescription(): Promise<void> {}
      public async setRemoteDescription(): Promise<void> { this.ontrack?.({ track: { kind: "video" } }); }
      public close(): void {}
    }
    const sources = new WeakMap<HTMLMediaElement, unknown>();
    Object.defineProperty(HTMLMediaElement.prototype, "srcObject", {
      configurable: true,
      get(): unknown { return sources.get(this); },
      set(source: unknown): void {
        sources.set(this, source);
        queueMicrotask(() => this.dispatchEvent(new Event("loadeddata")));
      },
    });
    Object.defineProperty(HTMLMediaElement.prototype, "play", { configurable: true, value: async (): Promise<void> => undefined });
    Object.assign(window, { MediaStream: FakeMediaStream, RTCPeerConnection: FakePeerConnection });
  });
}

interface RelayCalls {
  deletes: string[];
  registrations: string[];
  whepOffers: string[];
  dispenseBodies: string[];
}

async function mockRelay(page: Page): Promise<RelayCalls> {
  const deletes: string[] = [];
  const registrations: string[] = [];
  const whepOffers: string[] = [];
  const dispenseBodies: string[] = [];
  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const json = (body: unknown, status = 200): Promise<void> => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/home") return json({ status: { relay: { status: "running", uptime_seconds: 1 }, local_mqtt: { connected: true }, devices: { known: 1, local_online: 1, cloud_online: 1 } }, devices: [{ device_id: DEVICE_ID, product_id: "PLAF203", local_state: "LOCAL_ONLINE", last_seen_at: 1, rssi: -42, schedule: [{ execution_time: "07:30", grain_num: 3, repeat_day: [1] }], camera: { available: true, bridge_registered: true, go2rtc_reachable: true, online: true, media_consumers: 0 } }] });
    if (url.pathname === `/api/devices/${DEVICE_ID}/daily`) return json({ device: { device_id: DEVICE_ID, product_id: "PLAF203", local_state: "LOCAL_ONLINE", last_seen_at: 1, rssi: -42, schedule: [{ execution_time: "07:30", grain_num: 3, repeat_day: [1, 2, 3, 4, 5, 6, 7] }], camera: { available: true, bridge_registered: true, go2rtc_reachable: true, online: true, media_consumers: 0 } }, state: { desired: [], local_confirmed: [], schedule_plans: [] }, controls: {}, activity: [] });
    if (url.pathname === `/api/devices/${DEVICE_ID}/camera`) return json({ available: true, bridge_registered: true, go2rtc_reachable: true, online: true, media_consumers: 0 });
    if (url.pathname.includes("/camera/viewers/")) {
      if (request.method() === "POST") registrations.push(url.pathname);
      if (request.method() === "DELETE") deletes.push(url.pathname);
      return route.fulfill({ status: 204 });
    }
    if (url.pathname.endsWith("/camera/webrtc") && request.method() === "POST") {
      whepOffers.push(request.postData() ?? "");
      return route.fulfill({ status: 201, contentType: "application/sdp", headers: { "X-Relay-WebRTC-Session": "a".repeat(32) }, body: "answer" });
    }
    if (url.pathname.includes("/camera/webrtc/") && request.method() === "DELETE") return route.fulfill({ status: 204 });
    if (url.pathname === `/api/devices/${DEVICE_ID}/dispense` && request.method() === "POST") {
      dispenseBodies.push(request.postData() ?? "");
      return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    }
    return route.fulfill({ status: 404, contentType: "application/json", body: "{\"detail\":\"unexpected API request\"}" });
  });
  return { deletes, registrations, whepOffers, dispenseBodies };
}

test("Home and Camera use one player lifecycle per mounted page", async ({ page }) => {
  const errors: string[] = [];
  const failedResources: string[] = [];
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400) failedResources.push(`${response.status()} ${response.url()}`);
  });
  await mockMediaBrowser(page);
  const calls = await mockRelay(page);

  await page.goto("/");
  await page.waitForTimeout(100);
  expect(failedResources).toEqual([]);
  expect(errors).toEqual([]);
  await expect(page.getByRole("heading", { name: "PLAF203" })).toBeVisible();
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth === document.documentElement.clientWidth)).toBe(true);
  await expect(page.getByRole("link", { name: "Open feeder settings" })).toHaveAttribute("href", `/devices/${DEVICE_ID}/overview`);
  expect(calls.registrations).toHaveLength(1);
  expect(calls.whepOffers).toEqual(["offer"]);

  await page.waitForTimeout(3_250);
  expect(calls.registrations).toHaveLength(1);
  expect(calls.whepOffers).toHaveLength(1);

  await page.getByRole("button", { name: "Dispense now" }).click();
  await page.getByRole("button", { name: "Increase portions" }).click();
  await page.getByRole("button", { name: "Dispense", exact: true }).click();
  await expect.poll(() => calls.dispenseBodies).toEqual(["{\"grainNum\":2}"]);

  await page.evaluate(() => window.dispatchEvent(new Event("pagehide")));
  await expect.poll(() => calls.deletes.length).toBe(1);
  await page.getByRole("link", { name: "Open feeder settings" }).click();
  await expect(page.getByRole("heading", { name: "PLAF203" })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/devices/${DEVICE_ID}/overview$`));

  await page.goto(`/devices/${DEVICE_ID}/camera`);
  await expect(page.getByRole("heading", { name: "Camera" })).toBeVisible();
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
  expect(calls.registrations).toHaveLength(2);
  expect(calls.whepOffers).toHaveLength(2);

  for (let cycle = 0; cycle < 3; cycle += 1) {
    await page.evaluate(() => window.dispatchEvent(new Event("pagehide")));
    await expect.poll(() => calls.deletes.length).toBe(cycle + 2);
    await page.goto(`/devices/${DEVICE_ID}/camera`);
    await expect(page.getByText("Live", { exact: true })).toBeVisible();
  }
  expect(calls.registrations).toHaveLength(5);
  expect(calls.whepOffers).toHaveLength(5);
  expect(errors).toEqual([]);
});

test("Overview is a React route and upgrades legacy hash bookmarks", async ({ page }) => {
  await mockMediaBrowser(page);
  await mockRelay(page);

  await page.goto(`/devices/${DEVICE_ID}#overview`);
  await expect(page).toHaveURL(new RegExp(`/devices/${DEVICE_ID}/overview$`));
  await expect(page.getByRole("heading", { name: "PLAF203" })).toBeVisible();
  await expect(page.getByText("07:30 · 3 portions")).toBeVisible();
  await page.getByRole("link", { name: "Open camera" }).click();
  await expect(page.getByRole("heading", { name: "Camera" })).toBeVisible();
});

test("Camera keeps its viewer dormant when the relay reports it unavailable", async ({ page }) => {
  await mockMediaBrowser(page);
  const registrations: string[] = [];
  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === `/api/devices/${DEVICE_ID}/camera`) {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ available: false, online: false, bridge_registered: false, go2rtc_reachable: false, reason: "Camera bridge offline" }) });
    }
    if (url.pathname.includes("/camera/viewers/") && request.method() === "POST") registrations.push(url.pathname);
    return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });

  await page.goto(`/devices/${DEVICE_ID}/camera`);
  await expect(page.getByText("Camera unavailable", { exact: true })).toBeVisible();
  expect(registrations).toEqual([]);
});

test("Home keeps a single live preview when multiple feeder cards are visible", async ({ page }) => {
  await mockMediaBrowser(page);
  const registrations: string[] = [];
  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const json = (body: unknown, status = 200): Promise<void> => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/home") {
      return json({ status: { relay: { status: "running", uptime_seconds: 1 }, local_mqtt: { connected: true }, devices: { known: 2, local_online: 2, cloud_online: 2 } }, devices: [DEVICE_ID, "device-b"].map((device_id) => ({ device_id, product_id: "PLAF203", local_state: "LOCAL_ONLINE", last_seen_at: 1, rssi: -42, schedule: [], camera: { available: true, bridge_registered: true, go2rtc_reachable: true, online: true, media_consumers: 0 } })) });
    }
    if (url.pathname.includes("/camera/viewers/") && request.method() === "POST") {
      registrations.push(url.pathname);
      return route.fulfill({ status: 204 });
    }
    if (url.pathname.endsWith("/camera/webrtc") && request.method() === "POST") {
      return route.fulfill({ status: 201, contentType: "application/sdp", headers: { "X-Relay-WebRTC-Session": "b".repeat(32) }, body: "answer" });
    }
    if (request.method() === "DELETE" || request.method() === "PUT") return route.fulfill({ status: 204 });
    return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "PLAF203" })).toHaveCount(2);
  await expect.poll(() => registrations.length).toBe(1);
  expect(await page.evaluate(() => document.querySelectorAll("[aria-label='Live feeder camera']").length)).toBe(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth === document.documentElement.clientWidth)).toBe(true);
});

test("Home retains an active player when a later status poll fails", async ({ page }, testInfo: TestInfo) => {
  test.skip(testInfo.project.name !== "chromium", "The polling error lifecycle is viewport-independent.");
  await mockMediaBrowser(page);
  const registrations: string[] = [];
  let homeRequests = 0;
  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/home") {
      homeRequests += 1;
      if (homeRequests > 1) return route.fulfill({ status: 503, contentType: "application/json", body: "{\"detail\":\"Relay busy\"}" });
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: { relay: { status: "running", uptime_seconds: 1 }, local_mqtt: { connected: true }, devices: { known: 1, local_online: 1, cloud_online: 1 } }, devices: [{ device_id: DEVICE_ID, product_id: "PLAF203", local_state: "LOCAL_ONLINE", last_seen_at: 1, rssi: -42, schedule: [], camera: { available: true, bridge_registered: true, go2rtc_reachable: true, online: true, media_consumers: 0 } }] }) });
    }
    if (url.pathname.includes("/camera/viewers/") && request.method() === "POST") {
      registrations.push(url.pathname);
      return route.fulfill({ status: 204 });
    }
    if (url.pathname.endsWith("/camera/webrtc") && request.method() === "POST") return route.fulfill({ status: 201, contentType: "application/sdp", headers: { "X-Relay-WebRTC-Session": "c".repeat(32) }, body: "answer" });
    if (request.method() === "DELETE" || request.method() === "PUT") return route.fulfill({ status: 204 });
    return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });

  await page.goto("/");
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
  await expect(page.getByText("Updating feeder status failed. Live video is unchanged.")).toBeVisible({ timeout: 6_000 });
  expect(registrations).toHaveLength(1);
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
});
