"""Entrypoint for the PETLIBRO MQTT relay service."""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

from .config import RelayConfig
from .credential_capture_proxy import CredentialCaptureProxy
from .device_registry import SECONDS_PER_HOUR, DeviceIdentity, DeviceRegistry
from .local_responder import LocalResponder
from .logging_config import configure_logging
from .message_queue import MessageQueue
from .mqtt_bridge import MqttBridge, prime_local_subscription
from .observability.telemetry import RelayTelemetry
from .state_cache import StateCache
from .state_shadow import StateShadow
from .web.context import DashboardContext
from .web.server import DashboardServer

_LOGGER = logging.getLogger(__name__)

IDENTITY_POLL_INTERVAL_SECONDS = 3.0
IDENTITY_WAIT_LOG_EVERY_N_POLLS = 20  # ~1 minute at the interval above


def _install_shutdown_handler(stop_event: threading.Event) -> None:
    def _handle_shutdown_signal(signal_number: int, frame: FrameType | None) -> None:
        _LOGGER.info("Received signal %d, shutting down", signal_number)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)


def _resolve_identity(
    config: RelayConfig, registry: DeviceRegistry, stop_event: threading.Event
) -> DeviceIdentity | None:
    """Return the device identity to use, blocking until one is available.

    Uses the manually-configured identity from `.env` if all three fields are
    set. Otherwise waits for `CredentialCaptureProxy` to learn one from the
    feeder's own CONNECT packet, polling the registry until it does.

    Returns:
        The resolved `DeviceIdentity`, or `None` if `stop_event` was set
        before one became available (shutdown requested while waiting).
    """
    manual = config.manually_configured_identity()
    if manual is not None:
        client_id, username, password = manual
        _LOGGER.info("Using manually configured device identity: client_id=%s", client_id)
        return DeviceIdentity(client_id=client_id, username=username, password=password)

    _LOGGER.info(
        "No device identity configured - waiting for the feeder's first local connection to learn it "
        "(client_id, username, password captured automatically from its CONNECT packet)"
    )
    polls = 0
    while not stop_event.is_set():
        identity = registry.get_active()
        if identity is not None:
            _LOGGER.info("Using active device identity from registry: client_id=%s", identity.client_id)
            return identity
        polls += 1
        if polls % IDENTITY_WAIT_LOG_EVERY_N_POLLS == 0:
            _LOGGER.info("Still waiting for the feeder to connect locally...")
        stop_event.wait(IDENTITY_POLL_INTERVAL_SECONDS)
    return None


def main() -> None:
    """Load configuration and run the MQTT bridge until a shutdown signal is received."""
    config = RelayConfig.from_env()
    log_buffer = configure_logging(config.log_level)
    _LOGGER.info(
        "Starting petlibro-relay (upstream=%s:%d, local=%s:%d, capture-proxy=%s:%d)",
        config.upstream_host,
        config.upstream_port,
        config.local_host,
        config.local_port,
        config.capture_proxy_listen_host,
        config.capture_proxy_listen_port,
    )

    stop_event = threading.Event()
    _install_shutdown_handler(stop_event)

    state_cache = StateCache(config.state_cache_path)
    shadow = StateShadow(config.state_shadow_db_path)
    queue = MessageQueue(config.queue_db_path, config.max_queue_size)
    telemetry = RelayTelemetry()
    registry = DeviceRegistry(
        config.device_registry_db_path,
        retention_seconds=config.device_retention_hours * SECONDS_PER_HOUR,
    )
    registry.purge_expired()

    dashboard_context = DashboardContext(config, registry, queue, shadow, telemetry, log_buffer)
    dashboard_server: DashboardServer | None = None
    if config.web_enabled:
        dashboard_server = DashboardServer(dashboard_context, config.web_host, config.web_port)
        dashboard_server.start()

    capture_proxy = CredentialCaptureProxy(
        listen_host=config.capture_proxy_listen_host,
        listen_port=config.capture_proxy_listen_port,
        broker_host=config.local_host,
        broker_port=config.local_port,
        registry=registry,
    )
    capture_proxy.start()

    # Register the local subscription before the feeder can connect, so the
    # broker holds its opening burst for us instead of dropping it while we
    # are still waiting to learn the device's identity from that same burst.
    prime_local_subscription(config)

    try:
        identity = _resolve_identity(config, registry, stop_event)
        if identity is None:
            _LOGGER.info("Shutdown requested before a device identity was available")
            return

        responder = LocalResponder(
            config.local_responder, shadow, config.handled_msg_id_ttl_seconds
        )
        dashboard_context.set_active_device(identity, responder)
        if config.local_responder.enabled:
            _LOGGER.info(
                "Local responder enabled (ntp=%s, config=%s, feeding_plan=%s, tz=%s)",
                config.local_responder.ntp,
                config.local_responder.config,
                config.local_responder.feeding_plan,
                config.local_responder.device_timezone,
            )
        else:
            _LOGGER.info("Local responder disabled - relay is a pure pipe")

        bridge = MqttBridge(config, identity, state_cache, queue, responder, telemetry)
        bridge.run_forever()
        try:
            stop_event.wait()
        finally:
            bridge.stop()
    finally:
        if dashboard_server is not None:
            dashboard_server.stop()
        capture_proxy.stop()
        queue.close()
        registry.close()
        shadow.close()
        _LOGGER.info("Relay stopped")


if __name__ == "__main__":
    main()
