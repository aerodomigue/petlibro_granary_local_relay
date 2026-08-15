import { Navigate, Route, Routes } from "react-router-dom";
import type { JSX } from "react";

import { ErrorBoundary } from "./components/ErrorBoundary";
import { CameraPage } from "./features/camera/CameraPage";
import { HomePage } from "./features/home/HomePage";
import { SchedulePage } from "./features/schedules/SchedulePage";
import { SettingsPage } from "./features/settings/SettingsPage";
import { DeviceSettingsPage } from "./features/settings/DeviceSettingsPage";
import { AdvancedPage } from "./features/advanced/AdvancedPage";
import { ActivityPage } from "./features/activity/ActivityPage";
import { ApplicationLayout } from "./layouts/ApplicationLayout";
import { LegacyDeviceRedirect } from "./routes/LegacyDeviceRedirect";

export function App(): JSX.Element {
  return <ErrorBoundary><Routes><Route element={<ApplicationLayout />}><Route path="/" element={<HomePage />} /><Route path="/settings" element={<SettingsPage />} /><Route path="/devices/:deviceId" element={<LegacyDeviceRedirect />} /><Route path="/devices/:deviceId/camera" element={<CameraPage />} /><Route path="/devices/:deviceId/schedule" element={<SchedulePage />} /><Route path="/devices/:deviceId/activity" element={<ActivityPage />} /><Route path="/devices/:deviceId/settings" element={<DeviceSettingsPage />} /><Route path="/devices/:deviceId/advanced" element={<AdvancedPage />} /><Route path="/devices/:deviceId/:tab" element={<LegacyDeviceRedirect />} /><Route path="*" element={<Navigate to="/" replace />} /></Route></Routes></ErrorBoundary>;
}
