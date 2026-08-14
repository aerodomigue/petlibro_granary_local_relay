# Troubleshooting

This guide keeps diagnosis local and read-only. Do not paste a `.env` file,
database, MQTT CONNECT packet, camera UID, token or password into an issue or
log collector.

## Quick health check

From the deployment directory:

```bash
docker compose ps
curl -fsS http://127.0.0.1:8080/healthz
docker compose logs --tail=100 relay
```

`/healthz` describes the relay itself. PETLIBRO cloud instability alone does
not make a healthy local relay unhealthy.

## Feeder does not reach the relay

1. Confirm the feeder is on the intended 2.4 GHz IoT network.
2. Confirm the DNS rewrite points only the documented PETLIBRO MQTT hostnames
   to the relay VM IP and port 1883.
3. Confirm the relay host's own upstream DNS remains external; otherwise it
   would resolve the public broker hostname back to its local capture proxy.
4. Check `docker compose ps` and the relay logs for a learned device identity.

The relay captures a feeder's MQTT CONNECT locally so that manual credentials
are normally unnecessary. It does not need, and should not be given, account
passwords for this purpose.

## Dashboard cannot be opened

Verify the mapped dashboard port and the Compose service:

```bash
docker compose ps relay
curl -fsS http://127.0.0.1:8080/api/status
```

If it works on the host but not another LAN device, review the VM/network
policy and `PETLIBRO_WEB_HOST_PORT`. Keep this interface private; it contains
device state and diagnostics even though sensitive fields are redacted.

## Cloud shows reconnecting or offline

The upstream state is MQTT-level, not just TCP-level: `ONLINE` means a
successful CONNACK. PETLIBRO may accept TCP then never send a CONNACK. Inspect
the Cloud tab and relay logs before changing DNS or restarting the feeder.

When upstream is unavailable, feeder-to-cloud telemetry follows the existing
queue/replay policy. Interactive controls never wait in that queue for a later
surprise execution.

## Camera is unavailable or black

First inspect the feeder's Camera tab and Advanced diagnostics. Then check the
three local services:

```bash
docker compose ps camera-bridge go2rtc relay
docker compose logs --tail=120 camera-bridge
docker compose logs --tail=120 go2rtc
docker compose logs --tail=120 relay
```

Camera sidecars need host networking in the supplied topology. The relay must
use `PETLIBRO_CAMERA_BRIDGE_URL=http://host.docker.internal:8081` to reach the
host-networked bridge. A valid camera path is:

```text
viewer UUID -> WHEP -> go2rtc source -> RTSP consumer -> feeder stream
```

Closing the last viewer waits for the configured idle grace (10 seconds by
default), then stops the local source and feeder stream. If a stream remains
active, look for a remaining browser viewer before restarting services.

## Settings or schedule change did not save

The dashboard waits for the feeder acknowledgement. Keep the feeder locally
online, wait for the feedback message, and inspect Activity or relay logs.
Do not retry by manually publishing MQTT: there is intentionally no generic
MQTT endpoint. A failed schedule draft stays in the browser so it can be
corrected and retried.

## Safe restart and rollback

Use the deployment's normal Compose lifecycle only after recording the current
image/tag and checking `docker compose ps`. Restarting the relay preserves its
SQLite data under `/data`; deleting that volume removes learned identities,
queues and State Shadow information. Do not delete it as a first diagnostic
step.

For a clean functional rollback, revert the application image/commit through
your normal deployment process and retain the data volume. See [setup](setup.md)
for the stack topology and [camera](camera.md) for camera-specific boundaries.
