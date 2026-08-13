"""Background lifecycle wrapper for the narrow relay dashboard server."""

from __future__ import annotations

import logging
import threading

import uvicorn

from .app import create_app
from .context import DashboardContext

_LOGGER = logging.getLogger(__name__)
SERVER_START_TIMEOUT_SECONDS = 5.0


class DashboardServer:
    """Run FastAPI away from MQTT callback and delivery threads."""

    def __init__(self, context: DashboardContext, host: str, port: int) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(create_app(context), host=host, port=port, log_config=None, access_log=False)
        )
        self._thread = threading.Thread(target=self._server.run, name="web-dashboard", daemon=True)

    def start(self) -> None:
        """Start the HTTP server and return without blocking MQTT startup."""
        self._thread.start()
        _LOGGER.info("Dashboard started on HTTP listener")

    def stop(self) -> None:
        """Request a clean server stop during relay shutdown."""
        self._server.should_exit = True
        self._thread.join(timeout=SERVER_START_TIMEOUT_SECONDS)
