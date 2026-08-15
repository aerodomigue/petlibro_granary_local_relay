import { describe, expect, it } from "vitest";

import { parseSchedules } from "../src/api/schedules";
import type { DailyDeviceDetail } from "../src/types/api";

function detailWithPlans(plans: unknown[]): DailyDeviceDetail {
  return {
    device: { camera: { available: false, bridge_registered: false, go2rtc_reachable: false, media_consumers: 0, online: false }, device_id: "device-a", last_seen_at: null, local_state: "LOCAL_ONLINE", product_id: "PLAF203", rssi: null, schedule: [] },
    state: { desired: [], local_confirmed: [], schedule_plans: plans.map((plan) => ({ plan, source: "local", updated_at: 123 })) },
    controls: {},
    activity: [],
  };
}

describe("parseSchedules", () => {
  it("maps the daily API schedule projection into strict frontend types", () => {
    expect(parseSchedules(detailWithPlans([{ planId: -1, executionTime: "07:30", grainNum: 3, enableAudio: true, audioTimes: 2, repeatDay: [1, 2], syncTime: 50 }]))).toEqual([{ plan: { planId: -1, executionTime: "07:30", grainNum: 3, enableAudio: true, audioTimes: 2, repeatDay: [1, 2], syncTime: 50 }, source: "local", updatedAt: 123 }]);
  });

  it("drops malformed schedule records rather than rendering raw backend data", () => {
    expect(parseSchedules(detailWithPlans([{ planId: -1, executionTime: "07:30", grainNum: 3, enableAudio: true, audioTimes: 2, repeatDay: [1, 1] }]))).toEqual([]);
  });
});
