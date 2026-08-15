import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import type { JSX } from "react";

import { getDailyDevice } from "../../api/deviceDetails";
import { queryKeys } from "../../api/queryKeys";
import { DeviceNavigation } from "../../components/DeviceNavigation";
import type { ActivityEvent } from "../../types/api";

const ACTIVITY_REFRESH_MS = 10_000;

type ActivityTone = "failure" | "neutral" | "success";

function activityPresentation(event: ActivityEvent): { description: string; title: string; tone: ActivityTone } {
  if (event.kind === "feeder_dispensing") return { description: "The feeder reported dispensing activity.", title: "Dispensing activity", tone: "neutral" };
  if (event.kind === "feeder_error") return { description: "The feeder reported that it needs attention.", title: "Feeder needs attention", tone: "failure" };
  return { description: "A feeder update was recorded.", title: "Feeder update", tone: "neutral" };
}

function formatTimestamp(timestamp: number | null): string {
  if (timestamp === null) return "Time unavailable";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(timestamp * 1_000));
}

/** Present only feeder-facing activity supplied by the safe daily projection, never technical logs. */
export function ActivityPage(): JSX.Element {
  const { deviceId } = useParams();
  const activity = useQuery({ enabled: Boolean(deviceId), queryKey: queryKeys.activity(deviceId ?? ""), queryFn: ({ signal }) => getDailyDevice(deviceId ?? "", signal), refetchInterval: ACTIVITY_REFRESH_MS });
  if (!deviceId) return <p className="state-message state-message--error">Unknown feeder.</p>;
  if (!activity.data && activity.isPending) return <p className="state-message">Loading activity…</p>;
  if (!activity.data && activity.isError) return <p className="state-message state-message--error">Activity is unavailable: {activity.error.message}</p>;
  const entries = activity.data!.activity.slice().reverse();
  return <section aria-labelledby="activity-title"><header className="page-heading"><div><h1 id="activity-title">Activity</h1><p>Recent feeder events.</p>{activity.isError && <p className="refresh-warning" role="status">Updating activity failed. Showing the most recent available events.</p>}</div></header><DeviceNavigation active="activity" deviceId={deviceId} />{entries.length === 0 ? <section className="empty-state"><h2>No activity yet</h2><p>Feeder activity will appear here when it is reported.</p></section> : <ol aria-label="Recent feeder activity" className="activity-timeline">{entries.map((event, index) => { const presentation = activityPresentation(event); return <li className={`activity-timeline__item activity-timeline__item--${presentation.tone}`} key={`${event.timestamp ?? "unknown"}-${event.kind}-${index}`}><span aria-hidden="true">{presentation.tone === "success" ? "✓" : presentation.tone === "failure" ? "!" : "○"}</span><div><strong>{presentation.title}</strong><p>{presentation.description}</p><time dateTime={event.timestamp === null ? undefined : new Date(event.timestamp * 1_000).toISOString()}>{formatTimestamp(event.timestamp)}</time></div></li>; })}</ol>}</section>;
}
