import { Link, Navigate, Route, Routes, useParams } from "react-router-dom";
import type { JSX } from "react";

import { HomePage } from "./features/home/HomePage";

function DeviceMigrationPage(): JSX.Element {
  const { deviceId } = useParams();
  return <section className="migration-page"><Link to="/">← All feeders</Link><h1>Device migration preview</h1><p>React routing is active for <code>{deviceId}</code>. Device tabs will move here one by one while the legacy dashboard remains the stable UI.</p></section>;
}

export function App(): JSX.Element {
  return <main className="application"><Routes><Route path="/" element={<HomePage />} /><Route path="/devices/:deviceId" element={<Navigate to="overview" replace />} /><Route path="/devices/:deviceId/:tab" element={<DeviceMigrationPage />} /><Route path="*" element={<Navigate to="/" replace />} /></Routes></main>;
}
