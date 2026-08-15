# React frontend migration

The legacy dashboard remains the production UI while the React implementation
is developed on `feat/react-frontend`. This gives deployments an immediate
rollback path: use the existing inline dashboard until the React parity matrix
is complete and the new static bundle is explicitly enabled.

## Legacy inventory

| Feature | Legacy | React | Unit | E2E | VM |
| --- | --- | --- | --- | --- |
| Home device cards and status | Yes | Stabilized | Basic | Yes | Validated |
| Home camera auto-start | Yes | Stabilized | Lifecycle | Yes | Validated Live |
| Viewer UUID lifecycle | Yes | Stabilized | Lifecycle | Yes | Validated |
| Camera close / idle stop | Yes | Stabilized | Lifecycle | Yes | Validated (10 s grace) |
| Manual dispense | Yes | Stabilized, VM pending | Basic | Yes | Pending |
| Schedule list | Yes | Implemented | Yes | Yes | Validated read-only |
| Schedule create / edit | Yes | Implemented, feeder ACK required | Yes | Yes | Pending safe feeder mutation validation |
| Schedule enable / disable | Yes | Implemented, pessimistic | Yes | Yes | Pending safe feeder mutation validation |
| Schedule delete | Yes | Implemented, confirmed | Yes | Yes | Pending safe feeder mutation validation |
| Schedule polling-safe draft and focus | Partial | Implemented | Yes | Yes | Validated on live poll |
| Schedule mobile layout | Yes | Implemented | Yes | Yes | Validated at 390x844 |
| Global settings and Advanced preference | Yes | Implemented | Yes | Yes | Validated |
| Typed device settings | Yes | Implemented | Yes | Yes | Validated draft/poll; feeder write mock-only |
| Advanced diagnostics | Yes | Implemented, bounded safe projection | Yes | Yes | Validated |
| Activity timeline | Yes | Implemented from feeder-reported events only | Yes | Yes | Validated empty state |

## Coexistence and rollback

The backend APIs stay unchanged. React currently owns only Home and the
device Camera page. All unmigrated device tabs and global views deliberately
redirect to the legacy shell through `?ui=legacy`; they are not React
placeholders. Vite runs independently in development and
proxies `/api` to the relay. The final runtime will copy a static Vite build
into the existing Python image; Node will not run in production.

The migration must not switch the production shell or remove the legacy UI
until every row above has React parity, frontend tests, Playwright coverage and
a VM lifecycle validation. A failure in the new UI therefore rolls back by
selecting the legacy shell, without touching feeder, MQTT, camera, queue or
state data.

## Frontend state ownership

| State | Owner |
| --- | --- |
| Devices, daily detail, camera availability and activity | TanStack Query |
| Route, dialogs, view-only preferences | React / browser state |
| React dispense dialog | React local state and mutation state |
| Schedule lists | TanStack Query, device-scoped |
| Schedule create/edit draft | React Hook Form, owned by the open dialog |
| WebRTC peer connection, viewer UUID, retries and teardown | `CameraPlayer` hook |
| Advanced preference and browser-local feeder display name | `PreferencesProvider` / localStorage |
| Feeder settings form drafts | React Hook Form, one stable form per device and setting group |

Query refetches may update server data but never overwrite a dirty form or
create a media consumer. Home starts at most one intersection-visible preview;
the player does not remount during server polling. Camera lifecycle is
device-scoped and independent of the page polling cadence. A hidden tab is
released after its grace period and remains paused until an explicit reconnect.

## Schedule parity and constraints

React reads the safe `GET /api/devices/:id/daily` projection and sends only
the existing typed Schedule API payloads. Every create, edit, disable/enable
and delete action waits for the feeder acknowledgement; there is no optimistic
state or durable replay. The feeder accepts a complete schedule snapshot, so
the backend remains the sole owner of MQTT payload construction.

The legacy meaning of `repeatDay: []` is preserved as **Disabled**. React
remembers the previous days for a plan disabled in the current browser session.
After a browser reload, an unknown disabled plan opens the editor and requires
the user to choose its days before re-enabling it; the UI never invents a
schedule. The home view only marks meals as scheduled (`○`) because the daily
API does not yet expose a feeder-confirmed delivery event; it never infers
success merely from the current time.

The validation VM confirmed the direct Schedule route, the real feeder's
read-only list and a draft preserved with focus across multiple three-second
polls. Create, edit, enable/disable and delete have exhaustive mocked E2E
coverage, but remain explicitly pending a separate deliberately-safe feeder
mutation run.

## Settings, Advanced and Activity parity

Global Settings owns browser-local preferences only. Advanced diagnostics is
off by default and persists through the existing `petlibro-advanced-mode`
localStorage key. The technical route is hidden from device navigation and
does not fetch diagnostics while the preference is off. It is an interface
preference, not authorization; the dashboard remains intended for a trusted
LAN.

Device Settings uses the existing allowlisted, typed control endpoints. Each
group retains its React Hook Form draft and focus across the three-second
daily-data refresh. Feeder settings are submitted only through their fixed
group endpoint and retain the draft if the feeder rejects or times out the
change. The display name is explicitly browser-local and does not update the
PETLIBRO account.

The Advanced endpoint is a separate bounded projection: it includes only
diagnostic summaries and a limited set of sanitized per-device logs. It
excludes raw MQTT payloads, URLs, process paths, credentials, camera UIDs and
TUTK details. The normal Activity screen records only direct feeder reports
of dispensing activity and feeder errors. Technical relay/cloud telemetry is
excluded; Activity deliberately does not claim whether a meal succeeded until
the backend has a reliable outcome-aware feeding-history contract.

VM validation confirmed browser preference persistence across a reload, the
blocked Advanced route while the preference is off, and a Device Settings
draft retaining its value and keyboard focus across multiple live polls. It
intentionally made no feeder-setting write. Home and Camera both reached Live;
leaving Camera then produced viewer disconnect and a durable idle stream stop
after the ten-second grace period, with no phantom reconnect during the next
thirty seconds.

## Development commands

```bash
cd web
npm ci
npm run dev
npm run typecheck
npm test
npm run build
npx playwright test
```

The Vite development server listens on port 5173 and proxies `/api` to a relay
on port 8080 by default. Override `VITE_RELAY_URL` when the relay is elsewhere.
