# Camera bridge / TUTK POC

The Compose stack includes an internal `camera-bridge` and a pinned
`alexxit/go2rtc:1.9.14` sidecar. The relay learns a 20-character camera UID
from a feeder-originated `DEVICE_START_EVENT`, persists it in the State Shadow,
and asynchronously registers it to `camera-bridge`. The dashboard exposes only
safe readiness state at `GET /api/devices/{device_id}/camera`; it never returns
the UID, source URL, token, password, or a writable media API.

## Current status: discovery and knock verified; LOGIN and media unsupported

There is deliberately no `plaf203_<device_id>` source in
`go2rtc/go2rtc.yaml`. The `camera-bridge` API is limited to `GET /healthz`,
`GET /devices`, `PUT /devices/{device_id}`, `POST /devices/{device_id}/connect`,
`POST /devices/{device_id}/disconnect`, and `DELETE /devices/{device_id}`.
It records a UID mapping but explicitly reports
`plaf203_login_not_implemented`; it never opens a persistent session.

The bridge now implements the bounded, explicit connection preamble exposed by
`POST /devices/{device_id}/connect` and cancelled by
`POST /devices/{device_id}/disconnect`. It is never retried automatically.
`GET /devices` exposes a safe per-device state only: `idle`, `discovering`,
`knocking`, `logging_in`, `connected`, or `failed`, plus the most recent safe
error and transition timestamp. The UID is never returned.

The implementation is deliberately limited to the PLAF203 V3.0.30 exchange
confirmed in the local capture:

1. UDP `LAN_SEARCH3` is broadcast to port `32761`, carrying the requested
   20-character UID and a fresh 8-byte nonce.
2. The feeder answers from its dynamic UDP source port with `KNOCK2`. The
   bridge accepts the candidate only when **both** the UID and nonce match the
   request, then sends `KNOCK_RR2` back to that exact address.
3. The bridge enters `logging_in` and stops with
   `PLAF203 LOGIN is not enabled`.

No `connected` state is produced by the production connector at this stage.
The third-party fork documents a different `LOGIN A/B` shape from the local
V3.0.30 capture's post-knock exchange, so sending either would be an
unverified write to the feeder. The LOGIN primitive will be added only after a
fresh, device-specific capture establishes its exact packet layout and success
criterion.

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
these sources. Direct LAN RTSP is not documented. The video/audio codecs are
also unconfirmed for PLAF203; no transcoding is configured.

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
```

The API returns only safe state: `available`, `configured`, `online`,
`stream`, `webrtc`, `go2rtc_reachable`, `bridge_reachable`,
`bridge_registered`, `uid_learned`, and an explanatory `reason`. It never
returns a source URL, UID, token, password, or cloud contract.

For a configured device, the deterministic stream name is
`plaf203_<device_id>`. It supports the future multi-device mapping but is not
an instruction to invent a source. The next required evidence is a passive
capture of the exact V3.0.30 LOGIN response and authentication criterion,
followed separately by the AV control frames, media framing, and codecs. Only
then can a device-specific go2rtc producer or a verified upstream source be
considered.
