import { Link, Navigate, Route, Routes, useParams } from "react-router-dom";
import type { JSX } from "react";

import { ErrorBoundary } from "./components/ErrorBoundary";
import { HomePage } from "./features/home/HomePage";
import { ApplicationLayout } from "./layouts/ApplicationLayout";
import { LegacyDeviceRedirect } from "./routes/LegacyDeviceRedirect";

function DeviceMigrationPage(): JSX.Element {
  const { deviceId } = useParams();
  return <section className="migration-page"><Link to="/">← All feeders</Link><h1>Device migration preview</h1><p>React routing is active for <code>{deviceId}</code>. Device tabs will move here one by one while the legacy dashboard remains the stable UI.</p></section>;
}

export function App(): JSX.Element {
  return <ErrorBoundary><Routes><Route element={<ApplicationLayout />}><Route path="/" element={<HomePage />} /><Route path="/settings" element={<section className="migration-page"><h1>Settings migration preview</h1></section>} /><Route path="/devices/:deviceId" element={<LegacyDeviceRedirect />} /><Route path="/devices/:deviceId/:tab" element={<DeviceMigrationPage />} /><Route path="*" element={<Navigate to="/" replace />} /></Route></Routes></ErrorBoundary>;
}
