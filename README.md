# PETLIBRO Granary Local Relay

Local control and resilience for a PETLIBRO feeder that would otherwise depend
on a remote cloud service to remain useful.

The project currently targets the **PETLIBRO PLAF203 Granary Smart Camera
Feeder**. It is under active development; it does not claim support for every
PETLIBRO model.

## Why this project exists

Connected feeders depend heavily on PETLIBRO's servers. If that service is
unavailable, unstable, or eventually discontinued, a device someone owns can
lose important functions despite still being physically capable of doing them.

This project places a local relay between the feeder and PETLIBRO. It keeps the
official path available when it works, while adding local state, controls, and
camera access so the device is not wholly dependent on the cloud.

> The goal is to make locally owned hardware remain useful even when its cloud
> dependency is unavailable.

## How it works

```text
PETLIBRO device
       │
       ▼
PETLIBRO Local Relay
       │
       ├── Local control and state
       │
       └── PETLIBRO cloud
```

The PLAF203 firmware studied by this project connects to the PETLIBRO MQTT
infrastructure over an unencrypted MQTT connection. A LAN DNS override can
therefore direct that MQTT connection to the relay without breaking TLS. The
relay learns the feeder identity from its own MQTT CONNECT packet, forwards
normal traffic to PETLIBRO, and handles selected local work when the cloud is
not available.

This observation is a protocol/design weakness, not a claim of a demonstrated
exploit. Other PETLIBRO endpoints, including the official app's HTTPS traffic,
are outside this MQTT redirect and may use TLS.

```text
Normal
Feeder ─────────────────► PETLIBRO MQTT

With this project
Feeder ── DNS redirect ─► Local Relay ──► PETLIBRO MQTT
                              │
                              └──────────► Local logic
```

## Modes

### Relay mode

Relay mode is the primary, transparent mode. The feeder continues to work with
the official PETLIBRO app and cloud, but MQTT traffic passes through the local
relay. The relay records state, buffers eligible traffic during an outage, and
keeps supported local functions available without trying to replace a working
cloud service.

### Offline mode

Offline mode is the long-term continuity goal: local manual dispense,
schedules, basic configuration, device status, and the camera stream without
depending on PETLIBRO servers. The building blocks are present, but this is
**not yet a complete replacement for the official service or app**.

## Current features

- Automatic MQTT identity capture and enrollment; one relay can bridge multiple
  PLAF203 feeders.
- Transparent MQTT relay with separate cloud sessions, durable queues, replay
  policies, local state shadow, and cloud-outage telemetry.
- React dashboard with Home, device overview, camera, schedule, activity,
  settings, and an optional bounded Advanced view.
- ACK-backed local manual dispense, typed device settings, and persisted local
  feeding schedules. Interactive actions are never replayed later.
- Feature-flagged local fallback for confirmed NTP, cached configuration, and
  feeding-plan requests.
- Verified PLAF203 direct-LAN H.264 camera path:
  `camera-bridge → RTSP → go2rtc → WebRTC`, including multiple viewers and an
  on-demand idle lifecycle.

## Quick start

1. Copy the generic environment file and enable the features you need:

   ```sh
   cp .env.example .env
   # Edit .env: at minimum set PETLIBRO_WEB_ENABLED=true for the dashboard.
   ```

2. Start the stack:

   ```sh
   docker compose up -d --build
   ```

3. Point the feeder's MQTT hostnames at the LAN address of this machine with a
   DNS override, then reconnect the feeder. See [setup](docs/setup.md) for the
   exact hostnames and the required split-DNS safeguard.

4. Open `http://<relay-lan-ip>:8080/` when the dashboard is enabled.

## Architecture

```text
PLAF203 MQTT
     │
     ▼
credential-capture proxy → local Mosquitto → relay → PETLIBRO MQTT

PLAF203 camera
     │
     ▼
camera-bridge → RTSP → go2rtc → WebRTC → dashboard
```

The camera sidecars use host networking because PLAF203 direct-LAN UDP and
browser WebRTC candidates need the VM's real LAN addresses. The MQTT broker is
internal; the capture proxy on the relay container is the port exposed to the
feeder.

## Development status

The real-device protocol work is based on PLAF203 firmware V3.0.30 traffic and
captures. The project deliberately distinguishes observed behavior from
inference and keeps unknown commands conservative. Firmware changes can alter
undocumented behavior.

## Known limitations

- PLAF203 is the primary tested device; other PETLIBRO products are unverified.
- The offline mode is incomplete. The official mobile app, remote access,
  notifications, binding, OTA, Wi-Fi changes, and other cloud features are not
  locally reproduced.
- Browser video is validated. PLAF203 AAC transport has been captured and
  reassembled, but end-to-end browser audio is not yet claimed as validated.
- SD/HD profile switching is not enabled: observed profile traffic does not yet
  prove a stable quality mapping or resolution change.
- The dashboard has no authentication. Keep it on a trusted LAN and do not
  expose it to the Internet.

## Documentation

- [Setup and configuration](docs/setup.md)
- [Camera protocol and lifecycle](docs/camera.md)
- [Dashboard guide](docs/ui-parity.md)
- [Troubleshooting](docs/troubleshooting.md)

## Security and responsible use

Use this project only with devices and networks you own or are authorized to
administer. Never commit `.env`, captured MQTT identities, tokens, or private
camera data. The project is for local control, interoperability, resilience,
reverse engineering, and preservation of device functionality; it is not a
commercial PETLIBRO replacement or a way to bypass someone else's service.
