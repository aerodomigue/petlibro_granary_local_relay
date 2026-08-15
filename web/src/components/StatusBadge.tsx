import type { JSX, ReactNode } from "react";

interface StatusBadgeProps {
  tone: "online" | "offline" | "neutral";
  children: ReactNode;
}

export function StatusBadge({ tone, children }: StatusBadgeProps): JSX.Element {
  return <span className={`status-badge status-badge--${tone}`}>{children}</span>;
}
