# Camera bridge / TUTK POC

The Compose stack includes an internal `camera-bridge` and a pinned
`alexxit/go2rtc:1.9.14` sidecar. The relay learns a 20-character camera UID
from a feeder-originated `DEVICE_START_EVENT`, persists it in the State Shadow,
and asynchronously registers it to `camera-bridge`. The dashboard exposes
safe readiness state at `GET /api/devices/{device_id}/camera`; it never returns
the UID, RTSP source URL, token, password, or a generic media API.

## Current status: direct H.264 → RTSP → go2rtc → WebRTC

The `camera-bridge` API is limited to `GET /healthz`,
`GET /devices`, `PUT /devices/{device_id}`, `POST /devices/{device_id}/connect`,
`POST /devices/{device_id}/disconnect`, and `DELETE /devices/{device_id}`.
It serves a device-scoped, internal RTSP endpoint at
`rtsp://127.0.0.1:8554/device/<device_id>`. The relay registers the matching
`plaf203_<device_id>` source dynamically in official go2rtc and recreates it
after a sidecar restart. The browser exchanges SDP only with the relay at
`POST /api/devices/{device_id}/camera/webrtc`; it never receives an internal
source URL. H.264 is packetized and forwarded unchanged: no decoder, FFmpeg,
or transcoder is started.

The bridge implements the bounded, explicit connection preamble exposed by
`POST /devices/{device_id}/connect` and cancelled by
`POST /devices/{device_id}/disconnect`. RTSP is on-demand: the first go2rtc
consumer starts one feeder session, all WebRTC viewers share that producer, and
the final RTSP consumer schedules a bounded idle disconnect. A new consumer
cancels the pending idle stop. If an established feeder UDP session fails while
consumers remain, the bridge retries with a bounded 1, 2, 4, 8, then 10-second
backoff; it never retries an idle camera.
`GET /devices` exposes a safe per-device state only: `idle`, `discovering`,
`knocking`, `logging_in`, `connected`, `bootstrapping`, `streaming`, or
`failed`, plus the most recent safe error and transition timestamp. During a
streaming session it also reports only aggregate codec/frame/byte counters and
the last frame time. The UID and media payload are never returned.

The implementation is deliberately limited to the PLAF203 V3.0.30 exchange
confirmed in the local capture:

1. The relay observes the feeder IPv4 address from its local TCP CONNECT
   session, persists it alongside the learned UID, and registers both values
   with the internal bridge. A known address makes the bridge send UDP
   `LAN_SEARCH3` by unicast to `<feeder-ip>:32761`, carrying the requested
   20-character UID and a fresh 8-byte nonce. This avoids relying on Docker
   bridge-network broadcast delivery.
2. The feeder answers from a dynamic UDP source port with the 200-byte
   `LAN_SEARCH_R` (`0x0602`). The bridge validates the source IP and UID, then
   uses that dynamic source as the peer endpoint. It sends `LAN_SEARCH3` phase
   2 with the same nonce and then begins LOGIN; the response port is
   intentionally not required to be `32761`. This follows the legacy direct
   TUTK path. The generic remote path's `0x0402`/`0x0404` exchange is not part
   of this direct-LAN flow and is deliberately not emitted.
3. The bridge enters `logging_in`, sends the two captured Session16
   `client-start` datagrams (`0x0407`, 598 bytes each), and applies the same
   official TUTK partial wire transform as the preamble. The pair differs only
   by its fixed variant marker, sequence number, and timestamp.
4. The feeder must answer with the captured explicit `0x0408` / `0x0012`
   success acknowledgement (88 bytes after decoding), with the correlated
   Session16 ID and command bytes `00 21 0b`. Only then does the bridge retain
   the UDP transport and expose `connected`.
5. The bridge enters `bootstrapping` and repeats the captured client-start
   pair, sends the observed Session16 heartbeat, `SET_STREAM_CTRL` (`0x0024`,
   captured primary-profile payload), and `GET_FORMAT` (`0x032A`). It sends the observed
   acknowledgement shape immediately after `GET_FORMAT`, then waits for the
   matching `SET_STREAM_CTRL` reply (`0x0025`) on `0x1000` and `GET_FORMAT`
   reply (`0x032b`) on `0x7000` before sending `IPCAM_START` (`0x01ff`).
6. The bridge enters `streaming` **only** after a valid feeder Session16 media
   fragment reassembles into H.264 with an Annex-B NAL start code. The
   V3.0.30 capture confirms SPS/PPS/IDR and non-IDR video. A later official
   capture confirms AAC-LC ADTS at 44.1 kHz mono on media channel `0x0103`.

`POST /devices/{device_id}/disconnect` closes that authenticated transport.
`PETLIBRO_CAMERA_IDLE_TIMEOUT_SECONDS` controls the last-consumer grace period
and defaults to 10 seconds. Bootstrap and H.264 reassembly have fixed
timeouts, strict Session16 ID matching, bounded fragment/frame sizes, and
per-session isolation. Duplicate UDP fragments are ignored; incomplete or
malformed frames never create a streaming state.

The third-party fork's 570/572-byte login pair differs from the local V3.0.30
capture and is not used at runtime. The bridge uses only the pinned official
`github.com/AlexxIT/go2rtc/pkg/tutk` wire transform primitives.

The conclusion comes from these inspected upstream sources:

- `icex2/plaf203` `README.md`, *Camera support* section: it identifies the
  TUTK/Kalay SDK and says local video requires implementation of the server
  protocol spoken by the device.
- `icex2/plaf203/src/plaf203.py`, `TutkContractServiceOut.to_mqtt_payload()`:
  `TUTK_CONTRACT_SERVICE` carries cloud-issued `deviceTutkToken`,
  `deviceTutkUrl`, `contractId`, `startTime`, and `expires`. The project does
  not implement a PLAF203 media client or its AV handshake.
- go2rtc `v1.9.14` (commit `b5948cfb25404cc5cb37b166ecaa2dca20b11d4b`),
  `pkg/tutk/conn.go`, `Dial(host, uid, username, password)`: this is a
  generic TUTK transport/session primitive, not a registered `tutk://`
  source.
- go2rtc `internal/xiaomi/xiaomi.go` and
  `pkg/xiaomi/legacy/{client,producer}.go`, plus
  `pkg/wyze/{client,producer}.go`: each supported TUTK camera family adds its
  own authentication, AV control frames, packet framing, and codec handling
  above `tutk.Dial`. There is no PLAF203 equivalent in v1.9.14.
- go2rtc `internal/webrtc/webrtc.go` and `api/openapi.yaml` provide WebRTC
  output (`/api/webrtc?src=...`) when a producer exists. They cannot create a
  PLAF203 producer by themselves.

The official Go module `github.com/AlexxIT/go2rtc/pkg/tutk` is pinned by
`camera-bridge`. The bridge uses only its documented reversible wire transform
(`TransCodePartial` / `ReverseTransCodePartial`) for the verified UDP
preamble. Its public transport does not implement PLAF203's direct LAN
search/knock, LOGIN, bootstrap IOCtrl, or AV reassembly. A third-party fork
was inspected only as reverse-engineering evidence; it is not imported,
copied, or used at runtime.

The inspected fork has no Petlibro-specific Nebula/TUTK fallback after LAN
discovery. Its path is direct UDP LAN only. Conversely, upstream
`pkg/tutk.Dial` starts the generic Nebula/direct Kalay transport and does not
perform this PLAF203 preamble, so it is intentionally not used as a fallback.

Therefore, PLAF203 currently falls in category **D** for video credentials:
the observed MQTT contract is cloud-derived and dynamic. Whether media itself
uses direct LAN P2P (**B**) or a TUTK relay (**C**) is not determined from
these sources. Direct LAN RTSP is not documented. The direct-LAN capture
confirms H.264 video framing after bootstrap. Official traffic also confirms
AAC-LC ADTS audio. go2rtc keeps the bridge RTSP producer for H.264 and uses
a second, audio-only local RTSP consumer to transcode AAC to Opus for browser
WebRTC compatibility. Both local readers share the bridge's one feeder TUTK
session. The confirmed `AUDIOSTART` / `AUDIOSTOP` controls carry types
`0x0300` / `0x0301` and eight zero-valued ctrl-data bytes.

For offline analysis only, the camera bridge includes a PCAP summary tool. It
prints Session16 sequence/opcode details, bootstrap control payload prefixes,
and bounded H.264 fragment metadata without opening a socket:

```bash
cd camera-bridge
go run ./tools/plaf203-pcap -pcap /path/to/camera-open.pcap \
  -device-ip 10.3.100.90 -direct-only -session-only
```

## Docker networking and status

`camera-bridge` and official go2rtc use `network_mode: host`: the PLAF203
direct-LAN UDP peer and browser WebRTC ICE candidates must both use the VM's
real LAN addresses. `camera-bridge` owns RTSP port `8554`; go2rtc disables its
own RTSP listener and pulls `127.0.0.1:8554` instead. The dashboard still
proxies WHEP signaling through the relay. Keep this stack LAN-only; do not
expose the VM or go2rtc API to the Internet.

Enable the separate internal status and registration clients only when desired:

```dotenv
PETLIBRO_GO2RTC_ENABLED=true
PETLIBRO_GO2RTC_HOST=host.docker.internal
PETLIBRO_GO2RTC_PORT=1984
PETLIBRO_GO2RTC_TIMEOUT_SECONDS=1
PETLIBRO_GO2RTC_SOURCE_HOST=127.0.0.1
PETLIBRO_GO2RTC_SOURCE_PORT=8554
PETLIBRO_GO2RTC_RECONCILE_INTERVAL_SECONDS=5
PETLIBRO_CAMERA_BRIDGE_ENABLED=true
# Use this instead of HOST/PORT when camera-bridge uses host networking.
PETLIBRO_CAMERA_BRIDGE_URL=http://host.docker.internal:8081
PETLIBRO_CAMERA_BRIDGE_HOST=camera-bridge
PETLIBRO_CAMERA_BRIDGE_PORT=8081
PETLIBRO_CAMERA_BRIDGE_TIMEOUT_SECONDS=1
PETLIBRO_CAMERA_BRIDGE_RECONCILE_INTERVAL_SECONDS=5
PETLIBRO_CAMERA_DISCOVERY_BROADCAST_FALLBACK=true
PETLIBRO_CAMERA_IDLE_TIMEOUT_SECONDS=10
```

`PETLIBRO_CAMERA_DISCOVERY_BROADCAST_FALLBACK` defaults to `true`. Broadcast
is used only when no feeder IPv4 is known, or after a unicast discovery timeout
when this fallback remains enabled. A successful unicast sends no broadcast.
An IP update is an idempotent bridge registration and does not interrupt an
active camera session; a later explicit connection uses the updated address.
The relay also reconciles its persisted camera mappings against the bridge
registry every five seconds by default. This is optional with the bridge,
uses no camera UID from the bridge API, and makes a restarted sidecar converge
without requiring the feeder to reconnect.
`PETLIBRO_CAMERA_BRIDGE_URL`, when set, is the exclusive endpoint used for
health checks, registry reads, and registrations; it takes precedence over the
legacy `HOST` and `PORT` pair.

The API returns only safe state: `available`, `configured`, `online`,
`stream`, `webrtc`, `player_available`, `media_consumers`,
`go2rtc_reachable`, `bridge_reachable`, `bridge_registered`, `uid_learned`,
and an explanatory `reason`. It never returns a source URL, UID, token,
password, or cloud contract.

For a configured device, the deterministic stream name is
`plaf203_<device_id>`. One go2rtc producer pulls one bridge RTSP session; any
number of dashboard WebRTC consumers share it, so opening a second browser
does not open a second feeder session. When the last go2rtc RTSP consumer
disappears, the bridge stops the feeder session after the configurable idle
grace. The dashboard closes its `RTCPeerConnection` on page navigation and
after 15 seconds in a hidden tab; its reconnect delays are 1, 2, 5, then 10
seconds. When the first consumer starts, the bridge sends the captured
`AUDIOSTART` IOCtrl (`0x0300`) on channel `0x7000`; it sends `AUDIOSTOP`
(`0x0301`) once the final consumer leaves or the session closes. Audio remains
inside the same feeder session and is reassembled as AAC-LC ADTS (44.1 kHz,
mono) before go2rtc transcodes it to Opus for the existing browser WebRTC
PeerConnection. The bridge keeps the native media timestamp in diagnostics;
RTP audio timing follows the AAC 1024-sample access-unit cadence.
The currently observed video is 640×360 H.264. The player uses a custom overlay
for browser-local mute, volume, and fullscreen controls; it starts muted for
autoplay compatibility. The existing PeerConnection requests video and audio
on the same connection. Multiple observed `0x0024` profile payloads do not yet
establish an HD/SD switch mapping or a confirmed resolution change, so the
visible SD/HD controls are disabled and cannot send a camera command.
