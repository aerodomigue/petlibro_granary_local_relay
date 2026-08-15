import { Link, Outlet, useLocation } from "react-router-dom";
import type { JSX } from "react";

import { ErrorBoundary } from "../components/ErrorBoundary";

export function ApplicationLayout(): JSX.Element {
  const { pathname } = useLocation();
  return <><header className="app-header"><div><strong>PETLIBRO</strong><span>Your feeder, at a glance</span></div><nav aria-label="Main navigation"><Link to="/">Home</Link><Link to="/settings">Settings</Link></nav></header><main className="application"><ErrorBoundary resetKey={pathname}><Outlet /></ErrorBoundary></main></>;
}
