# React dashboard architecture

The dashboard is a single React/TypeScript application bundled with Vite and
served by FastAPI. The former inline dashboard was removed after route, form,
camera-lifecycle and real-device parity checks. There is no runtime frontend
switch and no `PETLIBRO_WEB_FRONTEND` setting.

## Final parity matrix

| Feature | React | Unit | E2E | VM |
| --- | --- | --- | --- | --- |
| Home, status and device cards | Yes | Yes | Yes | Validated |
| Home camera auto-preview | Yes | Lifecycle | Yes | Validated Live |
| Camera lifecycle and idle stop | Yes | Lifecycle | Yes | Validated (10 s grace) |
| Overview and device navigation | Yes | Yes | Yes | Validated |
| Manual dispense dialog | Yes | Yes | Yes | Mocked feeder ACK |
| Schedule list/create/edit/enable/delete | Yes | Yes | Yes | Read-only VM validation |
| Schedule and Settings polling-safe drafts | Yes | Yes | Yes | Validated |
| Activity | Yes | Yes | Yes | Validated |
| Global and device Settings | Yes | Yes | Yes | Validated without a feeder write |
| Advanced diagnostics | Yes | Yes | Yes | Validated and redacted |
| Mobile layouts | Yes | Yes | Yes | Validated at 390x844 |

Physical dispense, schedule mutation and settings mutation remain deliberately
mock-tested in this migration. They use existing ACK-backed APIs and are never
triggered by an automated VM test.

## Routes and compatibility

The canonical browser routes are `/`, `/settings` and
`/devices/:deviceId/{overview,camera,schedule,activity,settings,advanced}`.
Old device hash bookmarks such as `/devices/:deviceId#camera` are converted by
the client to their canonical route. Former global dashboard URLs (`/devices`,
`/cloud`, `/queues`, `/state`, `/ntp`, `/logs`, `/system`) historically showed
Home and now permanently redirect there.

Only browser routes receive the SPA shell. Every unknown `/api/*` route stays
an HTTP 404.

## State and refresh ownership

TanStack Query owns server projections. React Hook Form owns open Schedule and
Settings drafts; refetches never overwrite dirty values or focus. Camera state
(viewer UUID, peer connection, heartbeat, retry, AbortController and teardown)
lives in the shared `CameraPlayer` hook, so ordinary component rerenders never
create another media consumer.

The normal UI uses only `/api/home`, `/api/devices/:id/daily`,
`/api/devices/:id/camera`, and explicit typed action endpoints. Advanced uses
the bounded `/api/devices/:id/advanced` projection. Legacy raw diagnostics,
raw MQTT state, queue payloads and the global log stream are not public HTTP
endpoints anymore.

## Development

```sh
cd web
npm ci
npm run dev
npm run typecheck
npm test
npm run test:e2e
npm run build
```

Vite proxies `/api` to `http://127.0.0.1:8080` by default. Override
`VITE_RELAY_URL` for another local relay. The production Docker build uses
Node only for this build stage; FastAPI serves the generated static files.
