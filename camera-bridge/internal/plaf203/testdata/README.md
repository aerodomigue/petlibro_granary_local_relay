# PLAF203 login fixtures

`login_success_wire.hex` is the complete 88-byte UDP login-success response
captured offline from a PLAF203 running firmware V3.0.30. It is stored in its
wire-transcoded form so tests exercise the same `ReverseTransCodePartial`
boundary as the production connector. The fixture contains only an ephemeral
session identifier; it contains no account, MQTT, or camera credential.

`lan_search_response_wire.hex` is the complete 200-byte UDP
`LAN_SEARCH_R` response captured from the same PLAF203 V3.0.30. It is also
stored in wire-transcoded form. After `ReverseTransCodePartial`, it has a
`0x0602` opcode and `0x0012` subtype, the UID at offsets `16..35`, and no
field confirmed as an echoed request nonce. The dynamic UDP source address is
the peer endpoint. The direct-LAN sequence continues with a client-emitted
`LAN_SEARCH3` phase 2 carrying the original nonce before LOGIN.

`keepalive_response_wire.hex` is the complete 24-byte `0x0428` keepalive
response captured during the V3.0.30 post-LOGIN bootstrap. Tests decrypt it,
preserve its envelope and echo payload, and verify the corresponding
`0x0427`/`0x0021` reply through the official TUTK transform.

`session25_counters_client_decoded.hex` and
`session25_counters_device_decoded.hex` are complete 52-byte Session25
`0x0900` counter packets from the official PLAF203 V3.0.30 capture. They have
already passed through `ReverseTransCodePartial`; tests deliberately parse
them directly to avoid applying the transform twice.
