"""FastAPI application exposing read-only relay diagnostics.

Every route is a GET that projects existing state. There is deliberately no
endpoint that publishes MQTT, feeds, reboots, resets, changes configuration or
alters enrollment - not hidden, not undocumented, not behind a flag. The
dashboard is an observation surface only.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .context import DashboardContext
from .static import DASHBOARD_HTML

DEFAULT_LOG_LIMIT = 500
MAX_PAGE_SIZE = 500
SSE_WAIT_SECONDS = 15.0


def create_app(context: DashboardContext) -> FastAPI:
    """Create the dashboard application without adding any write routes."""
    app = FastAPI(title="PETLIBRO Local Relay", version="0.1.0", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> str:
        """Serve the self-contained dashboard UI."""
        return DASHBOARD_HTML

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Report relay health; PETLIBRO cloud availability is informational only.

        A cloud outage - which is the situation this relay exists for - must
        never make the relay itself look unhealthy, so `healthy` reflects only
        what this process controls.
        """
        status = context.status()
        summary = status["devices"]
        payload = {
            "healthy": True,
            "relay": True,
            "local_mqtt": status["local_mqtt"]["connected"],
            "devices_known": summary["known"],
            "devices_local_online": summary["local_online"],
            "upstream_petlibro_online": summary["cloud_online"],
        }
        return JSONResponse(payload, status_code=200)

    @app.get("/api/status")
    def status() -> dict[str, object]:
        """Return compact relay status aggregated over every device."""
        return context.status()

    @app.get("/api/cloud")
    def cloud() -> dict[str, object]:
        """Return each device's PETLIBRO MQTT state and recent upstream events."""
        return context.cloud()

    @app.get("/api/devices")
    def devices() -> dict[str, object]:
        """Return one row per known device plus aggregate counts."""
        return context.devices()

    @app.get("/api/devices/{device_id}")
    def device_detail(
        device_id: str, raw_limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE)
    ) -> dict[str, object]:
        """Return the full per-device view: cloud, queues, state, NTP."""
        detail = context.device_detail(device_id, raw_limit)
        if detail is None:
            raise HTTPException(status_code=404, detail="Unknown device")
        return detail

    @app.get("/api/queues")
    def queues(
        device_id: str, limit: int = Query(DEFAULT_LOG_LIMIT, ge=1, le=MAX_PAGE_SIZE)
    ) -> dict[str, object]:
        """Return bounded durable queue details for one device."""
        return context.queues(device_id, limit)

    @app.get("/api/state")
    def state(
        device_id: str, raw_limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE)
    ) -> dict[str, object]:
        """Return one device's shadow state."""
        return context.state(device_id, raw_limit)

    @app.get("/api/ntp")
    def ntp(device_id: str) -> dict[str, object]:
        """Return one device's NTP session-establishment observations."""
        return context.ntp(device_id)

    @app.get("/api/logs")
    def logs(limit: int = Query(DEFAULT_LOG_LIMIT, ge=1, le=MAX_PAGE_SIZE)) -> dict[str, object]:
        """Return recent sanitized logs from the process-local ring buffer."""
        return {"entries": context.logs.snapshot(limit)}

    @app.get("/api/logs/stream")
    def logs_stream(after: int = Query(0, ge=0)) -> StreamingResponse:
        """Stream sanitized log records with Server-Sent Events."""
        return StreamingResponse(
            _stream_logs(context, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/system")
    def system() -> dict[str, object]:
        """Return process and local database metadata."""
        return context.system()

    return app


def _stream_logs(context: DashboardContext, after: int) -> Iterator[str]:
    """Yield a bounded live log sequence with keepalives for idle clients."""
    sequence = after
    while True:
        entries = context.logs.wait_after(sequence, SSE_WAIT_SECONDS)
        if not entries:
            yield ": keepalive\n\n"
            continue
        for entry in entries:
            sequence = int(entry["sequence"])
            encoded = json.dumps(entry, separators=(",", ":"))
            yield f"id: {sequence}\nevent: log\ndata: {encoded}\n\n"
