# petlibro-relay setup

Transparent MQTT proxy sitting between the PLAF203 feeder and
`mqtt.us.petlibro.com`. Three services:

- `mosquitto-config`: one-shot, runs `petlibro_relay.mosquitto_config` to
  render `mosquitto.conf` from the same env vars as everything else, onto a
  shared volume, then exits. The stock mosquitto image is never modified.
- `mosquitto`: local broker, internal only (no host port) - not reachable
  directly, started only once `mosquitto-config` has finished successfully.
- `relay`: publishes the port the feeder actually connects to. Runs
  `CredentialCaptureProxy` (learns the feeder's identity from its own
  CONNECT packet, then forwards raw bytes to `mosquitto`) and `MqttBridge`
  (bridges `mosquitto` and the real PETLIBRO cloud broker in both
  directions, with a durable on-disk queue so neither side blocks the other
  and any outage gets replayed once the destination is back).

You do **not** need to extract the feeder's MQTT credentials by hand - see
"Credentials" below.

## 1. Run it

```sh
docker compose up -d --build
docker compose logs -f relay
```

### Dashboard (optional)

Set `PETLIBRO_WEB_ENABLED=true` in `.env`, then open
`http://<relay-LAN-IP>:8080/`. The dashboard is read-only apart from a single
PLAF203 sound toggle that waits for a feeder ACK before reporting success. It
does not expose generic MQTT publishing or any other feeder control. It shows local
MQTT, every device's real PETLIBRO MQTT state (only `CONNACK 0` means
online), per-device queues, the device registry and its enrollment statuses,
state shadow, NTP observations and sanitized live logs. The Devices tab lists
all bridged feeders with a per-device drill-down.

The compose file publishes port 8080 by default, but no HTTP process listens
until the flag is enabled. Keep this listener on the LAN only; it includes
device IDs, topics and internal diagnostics and must never be exposed to the
Internet.

On a fresh install, with no device identity configured in `.env` (the
default), expect:

```
Credential capture proxy listening on 0.0.0.0:1883, forwarding to mosquitto:1883
No enrolled devices yet - waiting for a feeder to connect locally so its identity can be learned from its own CONNECT packet
Connected to local broker (reason=Success)
```

...then, once a feeder has connected through the proxy at least once (see
steps 2-3 below):

```
Feeder connection from ('<feeder LAN IP>', <port>)
Learned device identity: client_id=<CLIENT_ID> product=PLAF203 is now enrolled
Device <CLIENT_ID> (product=PLAF203) is now bridged by this relay
Started upstream session for <CLIENT_ID> (product=PLAF203)
UPSTREAM online device=<CLIENT_ID> downtime=0.0s state_before=MQTT_CONNECTING
Upstream subscription dl/PLAF203/<CLIENT_ID>/device/service/sub -> granted (code=...)
```

Plugging in a second feeder needs no configuration change and no restart: it
is learned, enrolled and given its own upstream session as soon as it
connects. On a subsequent start, every enrolled device is restored up front:

```
Restored 2 enrolled device(s) from the registry
```

If you would rather approve new devices yourself, set
`PETLIBRO_AUTO_ENROLL=false`; a newly seen feeder is then recorded as a
candidate, shown on the dashboard, and not bridged.

If a subscription line says `denied`, that category isn't allowed for this
device identity - harmless, some server->device pushes still arrive over the
session's pre-existing subscription (see project notes).

## 2. Redirect the feeder to the local broker (DNS override)

The feeder resolves its MQTT host itself and connects in plain MQTT (no TLS
on port 1883), so a DNS override is enough - no certificates, no on-device
changes.

Our own capture of this device (firmware 3.0.30) only ever queried
`mqtt.us.petlibro.com`. The independently reverse-engineered
[`icex2/plaf203`](https://github.com/icex2/plaf203) project documents two
additional fallback hostnames the firmware is built to try:

```
mqtt.us.petlibro.com      <- primary, confirmed on this device's own traffic
us-mqtt-0.aiotlibro.com   <- documented fallback (icex2), not observed here
us-mqtt-0.dl-aiot.com     <- documented fallback (icex2), not observed here
```

On your Pi-hole / dnsmasq / router, add a local DNS (`A`) record for **all
three** hostnames, pointing to the same LAN IP - the machine running the
`relay` container (that's the one publishing the port now; `mosquitto`
itself is internal-only):

```
mqtt.us.petlibro.com      -> <LAN IP of the mosquitto host>
us-mqtt-0.aiotlibro.com   -> <LAN IP of the mosquitto host>
us-mqtt-0.dl-aiot.com     -> <LAN IP of the mosquitto host>
```

Redirecting only the primary leaves an escape hatch: if the firmware ever
falls back to one of the other two on a failed connection, that attempt
would resolve straight to the real cloud, bypassing the proxy entirely.

> **The relay must not be caught by its own override.** It has to resolve
> `mqtt.us.petlibro.com` to the *real* cloud to do its job. If it resolved
> that name through the same LAN resolver, it would connect to its own local
> broker and bridge mosquitto to itself - the feeder would look connected
> while nothing ever reached PETLIBRO. `docker-compose.yml` therefore pins
> public resolvers on the `relay` service
> (`PETLIBRO_UPSTREAM_DNS_PRIMARY` / `_SECONDARY`, default `1.1.1.1` /
> `9.9.9.9`). Verify before switching the feeder over:
>
> ```sh
> docker exec petlibro-relay python -c \
>   "import socket; print(socket.gethostbyname('mqtt.us.petlibro.com'))"
> ```
>
> This must print a public AWS address, never your relay host's LAN IP. If
> your DNS override is scoped to the feeder's IP/VLAN only, this is moot -
> but check it anyway, it's the failure mode that looks like success.

### Safe upstream outage test

To test an upstream outage without ever looping the relay back into its own
capture proxy or local broker, point only the relay upstream at an unused
local port, for example:

```env
PETLIBRO_UPSTREAM_HOST=127.0.0.1
PETLIBRO_UPSTREAM_PORT=65534
```

Never use `127.0.0.1:1883`, `localhost:1883`, or `[::1]:1883`: port `1883`
is the capture-proxy/local-broker path and would create an MQTT loop. The
relay rejects those unsafe literal loopback configurations before starting
any device or upstream MQTT session.

Do **not** override any other `*.petlibro.com` / `*.dl-aiot.com` hostname
(REST API, camera/Kalay, or the `sit-svc.` / `demo-svc.` / `test.svc.`
staging hosts icex2 found in the firmware binary but that aren't expected in
normal production traffic) - only the MQTT hosts move local, everything else
keeps talking straight to the cloud as before.

## Controlled cloud-backlog replay

When a PETLIBRO MQTT session returns, device-to-cloud backlog is replayed as
background traffic rather than drained in one burst. New live feeder reports
are selected before old backlog rows. The defaults are a 1.5-second settling
period, then at most 5 replay messages/second per device and 20/second across
the relay. Jitter can add up to 15% spacing, but never exceeds those caps:

```env
PETLIBRO_REPLAY_RATE_PER_DEVICE=5
PETLIBRO_REPLAY_RATE_GLOBAL=20
PETLIBRO_REPLAY_START_DELAY=1.5
PETLIBRO_REPLAY_JITTER=0.15
```

Only durable device-to-cloud backlog is affected. Local interactive control
publishes, including the confirmed `soundSwitch` flow, are never queued or
rate-limited by this scheduler.

To roll back at any time: remove the three DNS overrides. No changes are
needed on the feeder itself - it will simply resolve the real cloud IPs
again on its next DNS lookup/reconnect.

## 3. Reconnect the feeder

Only power-cycle or reconnect the feeder's Wi-Fi **after** the DNS override
is in place and `docker compose logs -f relay` shows a healthy upstream
connection. Reconnecting it beforehand just has it talk to the real cloud
directly, same as always - harmless, but defeats the point of testing the
proxy.

## What this does and doesn't give you

- **Does**: keeps the app/cloud features (remote feed, settings, camera,
  notifications) working through a single choke point you control, logs and
  caches every message, and buffers traffic during a PETLIBRO cloud outage so
  it's replayed once the cloud is reachable again.
- **Doesn't**: give the feeder new autonomous offline behavior. The
  already-synced feeding schedule executes on-device regardless of
  connectivity (that's firmware, unrelated to this proxy) - this relay only
  affects the cloud-dependent features (manual feed, live settings changes,
  camera, notifications), which still require the upstream connection to
  eventually come back to take effect.

## Credentials

No manual extraction needed. `CredentialCaptureProxy` reads the feeder's MQTT
client ID / `DL_PRODUCT_KEY` / `DL_PRODUCT_SECRET` straight off its own
CONNECT packet the first time it connects through the proxy (MQTT 3.1 here
has no TLS, so it's plaintext on the wire either way) and stores it in
`device_registry.sqlite3` on the `relay-data` volume - survives container
restarts.

`PETLIBRO_DEVICE_CLIENT_ID` / `_USERNAME` / `_PASSWORD` in `.env` are only a
manual override, for running the relay before the feeder has ever connected
locally (e.g. during development, or if you already have the identity from
an earlier packet capture). If set, they take priority over whatever the
registry has learned. `.env` is git-ignored - never commit it.
