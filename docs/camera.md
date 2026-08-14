# Camera bridge / TUTK POC

The Compose stack includes an internal `camera-bridge` and a pinned
`alexxit/go2rtc:1.9.14` sidecar. The relay learns a 20-character camera UID
from a feeder-originated `DEVICE_START_EVENT`, persists it in the State Shadow,
and asynchronously registers it to `camera-bridge`. The dashboard exposes only
safe readiness state at `GET /api/devices/{device_id}/camera`; it never returns
the UID, source URL, token, password, or a writable media API.

## Current status: verified bootstrap and H.264 observation; no media output

There is deliberately no `plaf203_<device_id>` source in
`go2rtc/go2rtc.yaml`. The `camera-bridge` API is limited to `GET /healthz`,
`GET /devices`, `PUT /devices/{device_id}`, `POST /devices/{device_id}/connect`,
`POST /devices/{device_id}/disconnect`, and `DELETE /devices/{device_id}`.
It records a UID mapping and reports `plaf203_h264_observation_only`. The
bridge can keep an explicitly requested direct LAN session open long enough to
recognize bounded H.264 access units, but no camera media source, RTSP,
WebRTC, player, decoder, or transcoder is created.

The bridge now implements the bounded, explicit connection preamble exposed by
`POST /devices/{device_id}/connect` and cancelled by
`POST /devices/{device_id}/disconnect`. It is never retried automatically.
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
   uses that dynamic source as the peer endpoint. No response field has yet
   been confirmed as an echoed request nonce. The feeder then emits the
   parser's existing 52-byte `KNOCK2`; the bridge validates its UID and nonce,
   sends `KNOCK_RR2` back to that peer, then begins LOGIN. The 52-byte KNOCK2
   layout remains to be confirmed against a dedicated PCAP. The response port
   is intentionally not required to be `32761`.
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
   HD payload), and `GET_FORMAT` (`0x032A`). It sends the observed
   acknowledgement shape immediately after `GET_FORMAT`, then accepts only the
   two bounded control replies on channels `0x1000`/`0x7000` before sending
   `IPCAM_START` (`0x01ff`).
6. The bridge enters `streaming` **only** after a valid feeder Session16 media
   fragment reassembles into H.264 with an Annex-B NAL start code. The
   V3.0.30 capture confirms SPS/PPS/IDR and non-IDR video; it does not confirm
   AAC, so audio is intentionally not parsed or claimed.

`POST /devices/{device_id}/disconnect` closes that authenticated transport.
No automatic retry occurs. Bootstrap and H.264 reassembly have fixed
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
confirms H.264 video framing after bootstrap; audio remains unconfirmed and no
transcoding is configured.

For offline analysis only, the camera bridge includes a PCAP summary tool. It
prints Session16 sequence/opcode details, bootstrap control payload prefixes,
and bounded H.264 fragment metadata without opening a socket:

```bash
cd camera-bridge
go run ./tools/plaf203-pcap -pcap /path/to/camera-open.pcap \
  -device-ip 10.3.100.90 -direct-only -session-only
```

## Docker networking and status

The sidecar API remains internal to Compose at `http://go2rtc:1984`; port
1984 is not mapped to the host. `8555/tcp` and `8555/udp` are also not mapped
because this POC has no source or browser player. A future WebRTC player would
need explicitly designed ICE/network exposure; it must not be opened merely
to show status.

Enable the separate internal status and registration clients only when desired:

```dotenv
PETLIBRO_GO2RTC_ENABLED=true
PETLIBRO_GO2RTC_HOST=go2rtc
PETLIBRO_GO2RTC_PORT=1984
PETLIBRO_GO2RTC_TIMEOUT_SECONDS=1
PETLIBRO_CAMERA_BRIDGE_ENABLED=true
PETLIBRO_CAMERA_BRIDGE_HOST=camera-bridge
PETLIBRO_CAMERA_BRIDGE_PORT=8081
PETLIBRO_CAMERA_BRIDGE_TIMEOUT_SECONDS=1
PETLIBRO_CAMERA_DISCOVERY_BROADCAST_FALLBACK=true
```

`PETLIBRO_CAMERA_DISCOVERY_BROADCAST_FALLBACK` defaults to `true`. Broadcast
is used only when no feeder IPv4 is known, or after a unicast discovery timeout
when this fallback remains enabled. A successful unicast sends no broadcast.
An IP update is an idempotent bridge registration and does not interrupt an
active camera session; a later explicit connection uses the updated address.

The API returns only safe state: `available`, `configured`, `online`,
`stream`, `webrtc`, `go2rtc_reachable`, `bridge_reachable`,
`bridge_registered`, `uid_learned`, and an explanatory `reason`. It never
returns a source URL, UID, token, password, or cloud contract.

For a configured device, the deterministic stream name is
`plaf203_<device_id>`. It supports the future multi-device mapping but is not
an instruction to invent a source. The next evidence required before any
consumer is a repeatable capture validating format-reply semantics, frame
continuity, audio framing, and an explicit device-specific output design.
