export const queryKeys = {
  home: ["home"] as const,
  device: (deviceId: string) => ["device", deviceId] as const,
  camera: (deviceId: string) => ["device", deviceId, "camera"] as const,
  schedule: (deviceId: string) => ["device", deviceId, "schedule"] as const,
};
