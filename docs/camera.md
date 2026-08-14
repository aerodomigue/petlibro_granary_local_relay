# PLAF203 camera bridge

This document describes the direct-LAN camera path implemented and observed on
the PETLIBRO PLAF203 firmware V3.0.30. It is intentionally separate from the
MQTT relay: camera transport is proprietary UDP/TUTK-family traffic, while the
relay continues to use MQTT for feeder state and control.

## Scope and confidence

| Area | Status |
| --- | --- |
| Direct LAN discovery, login, bootstrap and H.264 video | Confirmed from captures and a live dashboard stream |
| One feeder session shared by multiple WebRTC viewers | Confirmed in the implementation and lifecycle tests |
| AAC-LC ADTS acquisition and RTSP publication | Confirmed from captures and bridge processing |
| Browser audio end-to-end | Not yet claimed as validated |
| SD/HD profile mapping | Unknown; controls remain disabled |
| Cloud/TUTK relay fallback and public Internet access | Not implemented |

The bridge is a LAN component. It does not expose TUTK UIDs, cloud contracts,
credentials, RTSP source URLs, or media payloads through the relay dashboard.

## Runtime architecture

```text
PLAF203 feeder
  └─ direct UDP camera protocol
       └─ camera-bridge (host network, API :8081, RTSP :8554)
            └─ H.264/AAC RTSP source
                 └─ go2rtc (API :1984, WebRTC/WHEP :8555)
                      └─ relay WHEP proxy
                           └─ dashboard WebRTC player
```

`camera-bridge` and `go2rtc` use host networking in the supplied Compose
topology. This keeps direct feeder UDP and browser ICE candidates on real LAN
addresses. The dashboard exchanges SDP only with the relay; the browser never
receives the internal RTSP URL.

## Confirmed direct-LAN protocol

The following is the observed V3.0.30 path. Packet sizes are useful diagnostic
markers, not an API guarantee.

1. The relay learns the feeder IPv4 address from its local MQTT connection and
   the camera UID from `DEVICE_START_EVENT`. Both are persisted in the State
   Shadow and registered with `camera-bridge`.
2. `camera-bridge` sends 88-byte `LAN_SEARCH3` phase 1 to
   `<feeder-ip>:32761`, with the expected UID and a fresh nonce.
3. The feeder returns a 200-byte `LAN_SEARCH_R` (`0x0602`) from a **dynamic**
   UDP source port. The bridge requires the expected source IP and UID, accepts
   that dynamic port as the peer, and sends `LAN_SEARCH3` phase 2 with the same
   nonce. It does not assume the response comes from port 32761.
4. The bridge sends the captured Session16 client-start/login datagrams and
   accepts the correlated `0x0408` / `0x0012` login acknowledgement only.
5. Bootstrap uses the captured Session25 counters and control exchange:
   `SET_STREAM_CTRL` (`0x0024`), `GET_FORMAT` (`0x032a`), their replies
   (`0x0025` on channel `0x1000`, `0x032b` on `0x7000`), then `IPCAM_START`
   (`0x01ff`). Session25 `0x0009` packets are counters/acknowledgements, not
   malformed short bodies. The bridge maintains the required sequence state.
6. The `0x0a08` / `0x0b00` control exchange and `0x0428` keepalive path are
   handled as observed. The latter receives the corresponding `0x0427` reply.
7. The session becomes streaming only after strict reassembly produces a valid
   Annex-B H.264 NAL unit. Captures contain SPS, PPS, IDR and non-IDR frames.

The wire preamble uses the pinned `github.com/AlexxIT/go2rtc/pkg/tutk`
reversible partial transform. The implementation does not import a third-party
PLAF203 media client or invent an unobserved KNOCK packet in the direct-LAN
sequence.

## Media and controls

Video remains direct H.264: it is reassembled in `camera-bridge`, published to
the internal RTSP endpoint, consumed by go2rtc, and delivered as WebRTC video
without video transcoding.

The feeder's audio controls are confirmed as `AUDIOSTART` (`0x0300`) and
`AUDIOSTOP` (`0x0301`) with eight zero bytes of control data. Audio packets are
AAC-LC ADTS, 44.1 kHz mono, and are handled within the same feeder session.
Do not treat this as a promise that every browser currently receives audible
audio: browser end-to-end audio remains a separate validation milestone.

Several `0x0024` payloads have been observed around profile changes, but no
reliable SD/HD-to-resolution mapping has been established. The dashboard
therefore leaves SD and HD unavailable rather than pretending to change a
profile.

## Viewer lifecycle

The relay owns logical viewer UUIDs, while go2rtc owns the shared media source:

```text
first viewer
  -> register viewer UUID
  -> ensure one go2rtc source
  -> WHEP/WebRTC connection
  -> one RTSP consumer and one feeder session

additional viewers
  -> additional WebRTC consumers, same feeder session

last viewer leaves or expires
  -> remove viewer
  -> wait idle grace (default 10 s)
  -> remove source / close RTSP / stop feeder stream
```

The frontend heartbeats an active UUID every five seconds. The backend expires
an unrefreshed viewer after 20 seconds, using the same removal path as an
explicit close. Navigation, page hide, and camera-tab teardown release the
viewer. A polling refresh must never create a player, a viewer UUID, or a WHEP
connection. A new viewer during the idle grace cancels the pending stop.

## Configuration

The camera sidecars are opt-in. The normal Compose values are:

```dotenv
PETLIBRO_CAMERA_BRIDGE_ENABLED=true
PETLIBRO_CAMERA_BRIDGE_URL=http://host.docker.internal:8081
PETLIBRO_GO2RTC_ENABLED=true
PETLIBRO_GO2RTC_HOST=host.docker.internal
PETLIBRO_GO2RTC_SOURCE_HOST=127.0.0.1
PETLIBRO_GO2RTC_SOURCE_PORT=8554
PETLIBRO_CAMERA_IDLE_TIMEOUT_SECONDS=10
# Optional host-network listeners; keep the RTSP ports aligned.
CAMERA_BRIDGE_LISTEN_ADDR=:8081
PETLIBRO_CAMERA_MEDIA_RTSP_LISTEN_ADDR=:8554
```

`PETLIBRO_CAMERA_BRIDGE_URL`, when set, is the only bridge endpoint used by
the relay for health checks and registration. It is necessary because the
camera bridge is host-networked while the relay remains in the Compose network.
If the bridge RTSP listener is changed, set `PETLIBRO_GO2RTC_SOURCE_PORT` to
the same port so go2rtc continues to consume the intended source.
`PETLIBRO_CAMERA_DISCOVERY_BROADCAST_FALLBACK` defaults to `true`, but unicast
is used whenever the relay knows the feeder address.

The safe relay status endpoint is `GET /api/devices/{device_id}/camera`.
It reports readiness and aggregate state only. For low-level offline analysis,
the fixture-oriented PCAP tool is available without opening a network socket:

```bash
cd camera-bridge
go run ./tools/plaf203-pcap -pcap /path/to/capture.pcap \
  -device-ip <feeder-ip> -direct-only -session-only
```

## Troubleshooting

Start with [setup](setup.md) and [troubleshooting](troubleshooting.md). For a
camera-specific outage, first distinguish bridge registration, go2rtc
reachability, WHEP negotiation, and direct feeder streaming in the dashboard's
Advanced diagnostics. Do not expose go2rtc, the bridge API, or RTSP to the
Internet.
