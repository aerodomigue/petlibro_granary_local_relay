"""Entrypoint for the PETLIBRO MQTT relay service."""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

from .config import RelayConfig
from .credential_capture_proxy import CredentialCaptureProxy
from .device_registry import DeviceIdentity, DeviceRegistry
from .logging_config import configure_logging
from .message_queue import MessageQueue
from .mqtt_bridge import MqttBridge
from .state_cache import StateCache

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
        identity = registry.get_most_recently_seen()
        if identity is not None:
            _LOGGER.info("Learned device identity from local traffic: client_id=%s", identity.client_id)
            return identity
        polls += 1
        if polls % IDENTITY_WAIT_LOG_EVERY_N_POLLS == 0:
            _LOGGER.info("Still waiting for the feeder to connect locally...")
        stop_event.wait(IDENTITY_POLL_INTERVAL_SECONDS)
    return None


def main() -> None:
    """Load configuration and run the MQTT bridge until a shutdown signal is received."""
    config = RelayConfig.from_env()
    configure_logging(config.log_level)
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
    queue = MessageQueue(config.queue_db_path, config.max_queue_size)
    registry = DeviceRegistry(config.device_registry_db_path)

    capture_proxy = CredentialCaptureProxy(
        listen_host=config.capture_proxy_listen_host,
        listen_port=config.capture_proxy_listen_port,
        broker_host=config.local_host,
        broker_port=config.local_port,
        registry=registry,
    )
    capture_proxy.start()

    try:
        identity = _resolve_identity(config, registry, stop_event)
        if identity is None:
            _LOGGER.info("Shutdown requested before a device identity was available")
            return

        bridge = MqttBridge(config, identity, state_cache, queue)
        bridge.run_forever()
        try:
            stop_event.wait()
        finally:
            bridge.stop()
    finally:
        capture_proxy.stop()
        queue.close()
        registry.close()
        _LOGGER.info("Relay stopped")


if __name__ == "__main__":
    main()
