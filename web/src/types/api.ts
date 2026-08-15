export interface RelayStatus {
  relay: { status: string; uptime_seconds: number };
  local_mqtt: { connected: boolean };
  devices: { known: number; local_online: number; cloud_online: number };
}

export interface CameraAvailability {
  available: boolean;
  bridge_reachable?: boolean;
  bridge_registered: boolean;
  go2rtc_reachable: boolean;
  online: boolean;
  media_consumers: number;
  webrtc?: boolean;
  reason?: string;
}

export interface DailySchedulePlan {
  execution_time: string;
  grain_num: number;
  repeat_day: number[];
}

export interface DailyDevice {
  device_id: string;
  product_id: string | null;
  local_state: "LOCAL_ONLINE" | string;
  last_seen_at: number | null;
  rssi: number | null;
  schedule: DailySchedulePlan[];
  camera: CameraAvailability;
}

export interface HomeResponse {
  status: RelayStatus;
  devices: DailyDevice[];
}

export type ScheduleDay = 1 | 2 | 3 | 4 | 5 | 6 | 7;

export interface Schedule {
  audioTimes: number;
  enableAudio: boolean;
  executionTime: string;
  grainNum: number;
  planId: number;
  repeatDay: ScheduleDay[];
  syncTime?: number;
}

export interface ScheduleSnapshot {
  plan: Schedule;
  source: "cloud" | "local" | string;
  updatedAt: number;
}

export interface ScheduleFormValues {
  audioTimes: number;
  enableAudio: boolean;
  executionTime: string;
  grainNum: number;
  repeatDay: ScheduleDay[];
  repeatMode: "every" | "custom" | "never";
}

export interface ScheduleCreateRequest {
  audioTimes: number;
  enableAudio: boolean;
  executionTime: string;
  grainNum: number;
  repeatDay: ScheduleDay[];
}

export type ScheduleUpdateRequest = Partial<ScheduleCreateRequest>;

export interface ScheduleMutationResult {
  cloud_sync_behavior: "confirmed" | "unknown";
  control: string;
  device_ack: boolean;
  device_id: string;
  success: boolean;
  value: { plans: Schedule[] };
}

export interface DailyDeviceDetail {
  device: DailyDevice;
  state: {
    schedule_plans: Array<{
      plan: unknown;
      source: unknown;
      updated_at: unknown;
    }>;
  };
}

export interface ScheduleData {
  device: DailyDevice;
  schedules: ScheduleSnapshot[];
}
