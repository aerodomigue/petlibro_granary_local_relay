import { Navigate, Route, Routes } from "react-router-dom";
import type { JSX } from "react";

import { ErrorBoundary } from "./components/ErrorBoundary";
import { CameraPage } from "./features/camera/CameraPage";
import { HomePage } from "./features/home/HomePage";
import { ApplicationLayout } from "./layouts/ApplicationLayout";
import { LegacyDeviceRedirect, LegacyGlobalRedirect } from "./routes/LegacyDeviceRedirect";

export function App(): JSX.Element {
  return <ErrorBoundary><Routes><Route element={<ApplicationLayout />}><Route path="/" element={<HomePage />} /><Route path="/settings" element={<LegacyGlobalRedirect path="settings" />} /><Route path="/devices/:deviceId" element={<LegacyDeviceRedirect />} /><Route path="/devices/:deviceId/camera" element={<CameraPage />} /><Route path="/devices/:deviceId/:tab" element={<LegacyDeviceRedirect />} /><Route path="*" element={<Navigate to="/" replace />} /></Route></Routes></ErrorBoundary>;
}
