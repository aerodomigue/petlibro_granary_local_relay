# Dashboard guide

The dashboard is a LAN-facing control and observability interface. It uses the
relay APIs only; it does not expose arbitrary MQTT publishing or raw device
credentials.

## Home

Home shows one card per enrolled feeder: presence, Wi-Fi, today's feeding
plans, the next meal and the most useful daily state. A camera-capable card
starts its compact preview automatically while it is visible. The preview uses
the same viewer UUID and WebRTC lifecycle as the full Camera tab; status
polling preserves that player instead of recreating it.

The gear icon on a card opens that feeder's Overview page. Leaving Home closes
the corresponding viewer, so the preview cannot keep a feeder stream alive in
the background.

## Feeder pages

Each feeder has these tabs:

| Tab | Purpose |
| --- | --- |
| Overview | Daily state, next meal and concise device health |
| Camera | Live WebRTC player when the local camera path is available |
| Schedule | Create, edit, enable, disable or remove local feeding plans |
| Activity | Recent feeder and relay events |
| Settings | Typed, ACK-backed device settings and manual dispense |
| Advanced | Redacted diagnostics, shown only when Advanced mode is enabled |

The Camera player starts muted for browser autoplay policy. Mute, volume and
fullscreen are browser-local controls. SD/HD buttons deliberately remain
disabled until a real feeder profile mapping is validated.

## Changes and acknowledgements

The interface presents a feeder change as saved only after the feeder's
matching acknowledgement, not just after a local MQTT publish. Interactive
commands are never put into the durable cloud replay queue.

`soundSwitch` is the control whose feeder acknowledgement and official-app
sync have been validated. Other typed controls may be available locally, but
their official-cloud sync should not be assumed unless explicitly documented.
Manual dispense is intentionally local and acknowledgement-backed; it is never
queued for later execution.

Schedule edits remain local drafts until Save. A status poll updates cards and
the plan list but must not reset an open Create/Edit form, its focus or values.
After a failed acknowledgement, the draft remains available for correction or
retry.

## Player and polling safety

The frontend separates server state from active form drafts and camera state.
Periodic refreshes may update health and lists; they must not mount another
player, generate a new viewer UUID, recreate a peer connection, or overwrite
an active draft. Page navigation, `pagehide`, and a prolonged hidden tab share
an idempotent camera teardown path.

The dashboard is designed for desktop and mobile. Camera controls use touch-
sized targets on small screens; forms collapse to one column and device tabs
remain horizontally scrollable rather than clipping content.

## Advanced data

Advanced mode is for bounded diagnostics, not for secrets. It may show safe
connectivity counters, local presence, camera availability, state counts and a
small sanitized device-log projection. It never exposes MQTT payloads, queue
payloads, TUTK UIDs, tokens, RTSP URLs, internal paths or credentials. Keep
the dashboard on a trusted LAN.
