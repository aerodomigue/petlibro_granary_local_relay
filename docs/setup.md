# Setup and configuration

This guide installs the relay for a PLAF203 on a trusted LAN. It assumes a
Docker host reachable by the feeder and a DNS server/router where you can add
local records.

## What Compose starts

| Service | Purpose | Network exposure |
| --- | --- | --- |
| `mosquitto-config` | Renders the Mosquitto configuration from environment variables, then exits. | Internal |
| `mosquitto` | Local MQTT broker used by the relay. | Internal |
| `relay` | MQTT capture proxy, relay, local state, dashboard, and WebRTC signaling proxy. | MQTT `1883` and dashboard `8080` on the host by default |
| `camera-bridge` | Optional PLAF203 direct-LAN camera transport and RTSP server. | Host network; API `8081`, RTSP `8554` |
| `go2rtc` | Optional RTSP-to-WebRTC gateway. | Host network; API `1984`, WebRTC `8555` |

`camera-bridge` and `go2rtc` use host networking. Run the stack on a Linux VM
or host whose LAN address is reachable from both the feeder and dashboard
browsers. Do not expose their administrative ports to the Internet.

## 1. Create the environment file

```sh
cp .env.example .env
```

The relay learns each feeder's MQTT identity automatically after the DNS
override is in place. Do not put captured credentials in `.env` unless you
intentionally need a one-device development seed. `.env` is ignored by Git.

For the normal dashboard and local camera path, set at least:

```dotenv
PETLIBRO_WEB_ENABLED=true
PETLIBRO_CAMERA_BRIDGE_ENABLED=true
PETLIBRO_GO2RTC_ENABLED=true
PETLIBRO_CAMERA_BRIDGE_URL=http://host.docker.internal:8081
PETLIBRO_DEVICE_TIMEZONE=Europe/Paris
```

Use your own IANA timezone. `PETLIBRO_CAMERA_BRIDGE_URL` is the preferred
relay-to-bridge endpoint with the Compose host-network topology. It takes
precedence over the legacy bridge host/port pair.

The complete, generic variable reference is [`.env.example`](../.env.example).
It is the source of truth for defaults and includes queue, local responder,
dashboard, replay, and camera settings.

## 2. Start the stack

```sh
docker compose up -d --build
docker compose ps
docker compose logs -f relay
```

With no enrolled device, the relay should report that it is waiting for a
feeder connection. The dashboard is available at:

```text
http://<relay-lan-ip>:8080/
```

`/healthz` reports local relay health only. PETLIBRO cloud unavailability does
not make the local service unhealthy.

## 3. Redirect only MQTT DNS

Create local DNS `A` records pointing to the LAN IP of the Docker host:

```text
mqtt.us.petlibro.com      → <relay-lan-ip>
us-mqtt-0.aiotlibro.com   → <relay-lan-ip>
us-mqtt-0.dl-aiot.com     → <relay-lan-ip>
```

The first hostname is confirmed on the PLAF203 V3.0.30 traffic studied here.
The other two are documented firmware fallbacks and are redirected to prevent
an unexpected bypass.

Do **not** redirect PETLIBRO HTTPS, app, or camera/TUTK hostnames. This is not
a TLS interception setup; the currently studied MQTT connection is plain MQTT
on TCP/1883.

### Keep the relay's cloud DNS separate

The relay itself must resolve `mqtt.us.petlibro.com` to the real cloud, not
back to its own capture proxy. Compose pins public upstream resolvers through:

```dotenv
PETLIBRO_UPSTREAM_DNS_PRIMARY=1.1.1.1
PETLIBRO_UPSTREAM_DNS_SECONDARY=9.9.9.9
```

Before redirecting the feeder, verify that the relay resolves a public address:

```sh
docker exec petlibro-relay python -c \
  "import socket; print(socket.gethostbyname('mqtt.us.petlibro.com'))"
```

It must not return the LAN address of the relay host.

## 4. Reconnect the feeder

After the DNS records and stack are ready, reconnect the feeder's Wi-Fi or
power-cycle it. The capture proxy receives its MQTT CONNECT packet, stores the
identity in the SQLite registry, and starts a separate PETLIBRO upstream
session for that device.

With `PETLIBRO_AUTO_ENROLL=true` (the default), the new feeder is bridged
immediately. With it set to `false`, the feeder is stored as a candidate and is
not bridged automatically.

## Dashboard and local controls

The dashboard is LAN-only by design. It provides Home, per-device Overview,
Camera, Schedule, Activity, Settings, and an optional Advanced view.

The public dashboard API deliberately exposes only bounded screen projections
and typed actions. It never exposes raw MQTT/queue payloads or accepts
arbitrary MQTT topics, commands, or payloads. A supported local action succeeds only
after the feeder sends the matching acknowledgement; actions are not placed in
the durable cloud replay queue.

The set of controls and their cloud-sync confidence is exposed by the running
device API. `soundSwitch` is the only setting whose local control and official
app/cloud sync have been confirmed on the tested feeder. Other typed settings
may be device-ACK-backed while their cloud sync remains unconfirmed.

## Local responder and replay

The relay normally remains a transparent pipe. Local responses are opt-in:

```dotenv
PETLIBRO_LOCAL_RESPONDER=true
PETLIBRO_LOCAL_NTP=true
PETLIBRO_LOCAL_CONFIG=true
PETLIBRO_LOCAL_FEEDING_PLAN=true
```

When enabled, the responder answers only confirmed NTP, cached configuration,
and cached feeding-plan requests. It never invents dangerous commands such as
manual feeding, reboot, reset, OTA, Wi-Fi changes, or binding.

Eligible device-to-cloud backlog is persisted and replayed conservatively when
PETLIBRO returns. Interactive local controls are never replayed. Replay limits
are controlled by `PETLIBRO_REPLAY_RATE_PER_DEVICE`,
`PETLIBRO_REPLAY_RATE_GLOBAL`, `PETLIBRO_REPLAY_START_DELAY`, and
`PETLIBRO_REPLAY_JITTER`.

## Safe cloud-outage test

To test the relay without connecting it to the local MQTT listener, point only
the upstream at an unused port:

```dotenv
PETLIBRO_UPSTREAM_HOST=127.0.0.1
PETLIBRO_UPSTREAM_PORT=65534
```

Never use `127.0.0.1:1883`, `localhost:1883`, or `[::1]:1883`; those addresses
would point at a local MQTT path and create a loop. Startup rejects unsafe
loopback upstream configurations.

## Rollback

Remove the three MQTT DNS records. The feeder will resolve the official cloud
again on its next lookup or reconnect. No change is required on the feeder.

## Next steps

- [Camera protocol and lifecycle](camera.md)
- [Troubleshooting](troubleshooting.md)
