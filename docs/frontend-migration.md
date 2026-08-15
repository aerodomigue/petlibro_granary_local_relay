# React frontend migration

The legacy dashboard remains the production UI while the React implementation
is developed on `feat/react-frontend`. This gives deployments an immediate
rollback path: use the existing inline dashboard until the React parity matrix
is complete and the new static bundle is explicitly enabled.

## Legacy inventory

| Feature | Legacy | React | Unit | E2E | VM |
| --- | --- | --- | --- | --- |
| Home device cards and status | Yes | In progress | Basic | Todo | Todo |
| Home camera auto-start | Yes | Todo | Todo | Todo | Todo |
| Viewer UUID lifecycle | Yes | Todo | Todo | Todo | Todo |
| Camera close / idle stop | Yes | Todo | Todo | Todo | Todo |
| Manual dispense | Yes | Todo | Todo | Todo | Todo |
| Schedule list/create/edit/delete/enable | Yes | Todo | Todo | Todo | Todo |
| Activity timeline | Yes | Todo | Todo | Todo | Todo |
| Typed device settings | Yes | Todo | Todo | Todo | Todo |
| Advanced mode and diagnostics | Yes | Todo | Todo | Todo | Todo |

## Coexistence and rollback

The backend APIs stay unchanged. Vite runs independently in development and
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
| Schedule, settings and dispense edits | React Hook Form draft state |
| WebRTC peer connection, viewer UUID, retries and teardown | `CameraPlayer` hook |

Query refetches may update server data but never overwrite a dirty form or
create a media consumer. Camera lifecycle is device-scoped and independent of
the page polling cadence.

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
