# Camera / TUTK POC

The Compose stack includes an internal `alexxit/go2rtc:v1.9.14` sidecar. The
relay only queries its `GET /api/streams` endpoint and exposes the safe status
at `GET /api/devices/{device_id}/camera` and the device Camera tab. It does
not proxy go2rtc's administrative API, write its configuration, or publish
MQTT.

## Current status: source unsupported

There is deliberately no `plaf203_<device_id>` source in
`go2rtc/go2rtc.yaml`. No source URL or credential variables are documented
because the exact PLAF203 camera AV protocol is not confirmed.

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

Enable status polling only when desired:

```dotenv
PETLIBRO_GO2RTC_ENABLED=true
PETLIBRO_GO2RTC_HOST=go2rtc
PETLIBRO_GO2RTC_PORT=1984
PETLIBRO_GO2RTC_TIMEOUT_SECONDS=1
```

The API returns only safe state: `available`, `configured`, `online`,
`stream`, `webrtc`, `go2rtc_reachable`, and an explanatory `reason`. It never
returns a source URL, UID, token, password, or cloud contract.

For a configured device, the deterministic stream name is
`plaf203_<device_id>`. It supports the future multi-device mapping but is not
an instruction to invent a source. A next step needs a passive capture or
documented PLAF203 AV handshake showing the exact TUTK identifier,
authentication, control frames, media framing, and codecs. Only then can a
device-specific go2rtc producer or a verified upstream source be considered.
