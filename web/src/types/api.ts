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
