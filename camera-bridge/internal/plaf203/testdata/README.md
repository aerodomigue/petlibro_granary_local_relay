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
the peer endpoint. The nonce is strictly correlated by the following
`KNOCK2` packet at offsets `36..43`; the bridge then sends `KNOCK_RR2`.
