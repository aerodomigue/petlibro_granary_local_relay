"""FastAPI application exposing read-only relay diagnostics."""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import FastAPI, Query
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
        """Report relay health; PETLIBRO cloud availability is informational only."""
        status = context.status()
        payload = {
            "healthy": True,
            "relay": True,
            "local_mqtt": status["local_mqtt"]["connected"],
            "device_connected": status["device"] is not None,
            "upstream_petlibro": status["upstream"]["state"] == "ONLINE",
        }
        return JSONResponse(payload, status_code=200)

    @app.get("/api/status")
    def status() -> dict[str, object]:
        """Return compact relay status."""
        return context.status()

    @app.get("/api/cloud")
    def cloud() -> dict[str, object]:
        """Return PETLIBRO MQTT state and recent upstream events."""
        return context.cloud()

    @app.get("/api/devices")
    def devices() -> dict[str, object]:
        """Return active and candidate device metadata."""
        return context.devices()

    @app.get("/api/queues")
    def queues(limit: int = Query(DEFAULT_LOG_LIMIT, ge=1, le=MAX_PAGE_SIZE)) -> dict[str, object]:
        """Return bounded durable queue details."""
        return context.queues(limit)

    @app.get("/api/state")
    def state(raw_limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE)) -> dict[str, object]:
        """Return current active-device shadow state."""
        return context.state(raw_limit)

    @app.get("/api/ntp")
    def ntp() -> dict[str, object]:
        """Return NTP session-establishment observations."""
        return context.ntp()

    @app.get("/api/logs")
    def logs(limit: int = Query(DEFAULT_LOG_LIMIT, ge=1, le=MAX_PAGE_SIZE)) -> dict[str, object]:
        """Return recent sanitized logs from the process-local ring buffer."""
        return {"entries": context.logs.snapshot(limit)}

    @app.get("/api/logs/stream")
    def logs_stream(after: int = Query(0, ge=0)) -> StreamingResponse:
        """Stream sanitized log records with Server-Sent Events."""
        return StreamingResponse(
            _stream_logs(context, after), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
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
