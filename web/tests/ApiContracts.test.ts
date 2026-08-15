import { describe, expect, it } from "vitest";

import { parseDailyDeviceDetail, parseHomeResponse } from "../src/api/contracts";

const camera = { available: true, bridge_registered: true, go2rtc_reachable: true, media_consumers: 0, online: true };
const device = { device_id: "device-a", last_seen_at: 1, local_state: "LOCAL_ONLINE", product_id: "PLAF203", rssi: -42, schedule: [] };

describe("relay API contracts", () => {
  it("accepts the real daily camera placement at the response root", () => {
    expect(parseDailyDeviceDetail({ activity: [], camera, controls: {}, device, state: { desired: [], local_confirmed: [], schedule_plans: [] } }).camera).toEqual(camera);
  });

  it("rejects the obsolete nested daily camera shape before a route can crash", () => {
    expect(() => parseDailyDeviceDetail({ activity: [], controls: {}, device: { ...device, camera }, state: { desired: [], local_confirmed: [], schedule_plans: [] } })).toThrow("Unexpected daily device detail");
  });

  it("rejects malformed home cards before they can mount a player", () => {
    expect(() => parseHomeResponse({ devices: [{ ...device }], status: {} })).toThrow("Unexpected home device");
  });
});
