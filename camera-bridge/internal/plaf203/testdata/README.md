# PLAF203 login fixtures

`login_success_wire.hex` is the complete 88-byte UDP login-success response
captured offline from a PLAF203 running firmware V3.0.30. It is stored in its
wire-transcoded form so tests exercise the same `ReverseTransCodePartial`
boundary as the production connector. The fixture contains only an ephemeral
session identifier; it contains no account, MQTT, or camera credential.
