"""Entrypoint for the PETLIBRO MQTT relay service."""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

from .config import MissingConfigError, RelayConfig
from .logging_config import configure_logging
from .message_queue import MessageQueue
from .mqtt_bridge import MqttBridge
from .state_cache import StateCache

_LOGGER = logging.getLogger(__name__)


def _install_shutdown_handler(stop_event: threading.Event) -> None:
    def _handle_shutdown_signal(signal_number: int, frame: FrameType | None) -> None:
        _LOGGER.info("Received signal %d, shutting down", signal_number)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)


def main() -> None:
    """Load configuration and run the MQTT bridge until a shutdown signal is received."""
    try:
        config = RelayConfig.from_env()
    except MissingConfigError as error:
        # Logging isn't configured yet without a valid config; fail loudly on stderr instead.
        raise SystemExit(f"Configuration error: {error}") from error

    configure_logging(config.log_level)
    _LOGGER.info(
        "Starting petlibro-relay for device %s (upstream=%s:%d, local=%s:%d)",
        config.device_client_id,
        config.upstream_host,
        config.upstream_port,
        config.local_host,
        config.local_port,
    )

    state_cache = StateCache(config.state_cache_path)
    queue = MessageQueue(config.queue_db_path, config.max_queue_size)
    bridge = MqttBridge(config, state_cache, queue)

    stop_event = threading.Event()
    _install_shutdown_handler(stop_event)

    bridge.run_forever()
    try:
        stop_event.wait()
    finally:
        bridge.stop()
        queue.close()
        _LOGGER.info("Relay stopped")


if __name__ == "__main__":
    main()
