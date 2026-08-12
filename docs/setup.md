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

With no device identity configured in `.env` (the default), expect:

```
Credential capture proxy listening on 0.0.0.0:1883, forwarding to mosquitto:1883
No device identity configured - waiting for the feeder's first local connection to learn it
Connected to local broker (reason=Success)
```

...then, once the feeder has connected through the proxy at least once (see
steps 2-3 below):

```
Feeder connection from ('<feeder LAN IP>', <port>)
Learned device identity: client_id=<CLIENT_ID>
Connected to upstream PETLIBRO broker (reason=Success)
Upstream subscription dl/PLAF203/<CLIENT_ID>/device/service/sub -> granted (code=...)
```

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

Do **not** override any other `*.petlibro.com` / `*.dl-aiot.com` hostname
(REST API, camera/Kalay, or the `sit-svc.` / `demo-svc.` / `test.svc.`
staging hosts icex2 found in the firmware binary but that aren't expected in
normal production traffic) - only the MQTT hosts move local, everything else
keeps talking straight to the cloud as before.

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
