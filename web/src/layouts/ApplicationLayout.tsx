import { Link, Outlet } from "react-router-dom";
import type { JSX } from "react";

export function ApplicationLayout(): JSX.Element {
  return <><header className="app-header"><div><strong>PETLIBRO</strong><span>Your feeder, at a glance</span></div><nav aria-label="Main navigation"><Link to="/">Home</Link><Link to="/settings">Settings</Link></nav></header><main className="application"><Outlet /></main></>;
}
