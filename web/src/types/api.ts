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
  /** Detailed camera diagnostics only; daily projections deliberately omit it. */
  media_consumers?: number;
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
  /**
   * The device detail projection intentionally keeps camera availability at
   * the response root. Home embeds it in each device card, while /daily does
   * not. Keeping that difference explicit prevents a runtime-only mismatch.
   */
  device: Omit<DailyDevice, "camera">;
  camera: CameraAvailability;
  state: {
    desired: SettingEntry[];
    local_confirmed: SettingEntry[];
    schedule_plans: Array<{
      plan: unknown;
      source: unknown;
      updated_at: unknown;
    }>;
  };
  controls: ControlCapabilities;
  activity: ActivityEvent[];
}

export type SettingValue = boolean | number | string;

export interface SettingEntry {
  key: string;
  value: SettingValue;
}

export interface ControlCapability {
  control: string;
  writable: boolean;
  device_ack_confirmed: boolean;
  cloud_sync_confirmed: boolean;
  device_online: boolean;
  required_state_available: boolean;
  pending: boolean;
}

export type ControlCapabilities = Record<string, ControlCapability> & { counters?: Record<string, number> };

export interface ActivityEvent {
  kind: "feeder_dispensing" | "feeder_error";
  /** Unix epoch seconds, as provided by relay telemetry. */
  timestamp: number | null;
}

export interface AdvancedDeviceDetail {
  device: {
    device_id: string;
    product_id: string | null;
    firmware: string | null;
    mac: string | null;
    ip: string | null;
    rssi: number | null;
    local_state: string;
    cloud_state: string;
    last_seen_at: number | null;
  };
  connectivity: AdvancedConnectivity;
  camera: AdvancedCamera;
  relay: AdvancedRelay;
  state_summary: AdvancedStateSummary;
  logs: AdvancedLogEntry[];
}

export interface AdvancedConnectivity {
  upstream_state: string | null;
  availability: Record<"15m" | "1h" | "24h", number>;
  counters: Record<string, number>;
  outage: {
    started_at: number | null;
    downtime_seconds: number | null;
    attempts: number;
    failed_attempts: number;
    last_reason: string | null;
  };
  queue_pending: number;
}

export interface AdvancedCamera {
  available: boolean;
  online: boolean;
  webrtc: boolean;
  bridge_registered: boolean;
  go2rtc_reachable: boolean;
  media_consumers: number;
  reason: string | null;
  uid_learned: boolean;
}

export interface AdvancedRelay {
  local_responder: boolean;
  ntp_enabled: boolean;
  config_enabled: boolean;
  feeding_plan_enabled: boolean;
}

export interface AdvancedStateSummary {
  reported_values: number;
  desired_values: number;
  local_confirmed_values: number;
  schedule_plans: number;
}

export interface AdvancedLogEntry {
  component: string;
  device_id?: string;
  level: string;
  message: string;
  timestamp: number | string | null;
}

export interface ScheduleData {
  device: Omit<DailyDevice, "camera">;
  schedules: ScheduleSnapshot[];
}
