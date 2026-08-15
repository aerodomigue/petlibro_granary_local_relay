export const queryKeys = {
  home: ["home"] as const,
  device: (deviceId: string) => ["device", deviceId] as const,
  dailyDevice: (deviceId: string) => ["device", deviceId, "daily"] as const,
  camera: (deviceId: string) => ["device", deviceId, "camera"] as const,
  schedule: (deviceId: string) => ["device", deviceId, "schedule"] as const,
  activity: (deviceId: string) => ["device", deviceId, "activity"] as const,
  advanced: (deviceId: string) => ["device", deviceId, "advanced"] as const,
  settings: (deviceId: string) => ["device", deviceId, "settings"] as const,
};
