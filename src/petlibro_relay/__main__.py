"""Entrypoint for the PETLIBRO MQTT relay service."""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

from .config import RelayConfig
from .credential_capture_proxy import CredentialCaptureProxy, DeviceSessionListener
from .device_manager import DeviceManager
from .device_presence import DevicePresenceTracker
from .device_registry import SECONDS_PER_HOUR, DeviceIdentity, DeviceRegistry
from .logging_config import configure_logging
from .message_queue import MessageQueue
from .mqtt_bridge import MqttBridge, prime_local_subscription
from .observability.telemetry import RelayTelemetry
from .state_cache import StateCache
from .state_shadow import StateShadow
from .web.context import DashboardContext
from .web.server import DashboardServer

_LOGGER = logging.getLogger(__name__)


class DeviceEnroller(DeviceSessionListener):
    """Turns a learned local session into a bridged device, and back.

    This is the runtime half of enrollment: the registry decides *whether* a
    device may be bridged, and this listener acts on that decision the moment
    the device connects, so no restart is needed to pick up a new feeder.
    """

    def __init__(
        self,
        registry: DeviceRegistry,
        devices: DeviceManager,
        presence: DevicePresenceTracker,
        bridge_holder: "BridgeHolder",
    ) -> None:
        """Wire the enroller to the components a new device has to reach.

        Args:
            registry: Source of truth for which devices may be bridged.
            devices: Owner of each bridged device's context.
            presence: Tracker recording which devices are locally connected.
            bridge_holder: Indirection to the bridge, which is built after
                this listener so the proxy can start accepting first.
        """
        self._registry = registry
        self._devices = devices
        self._presence = presence
        self._bridge_holder = bridge_holder

    def device_session_opened(self, identity: DeviceIdentity, peer_address: str) -> None:
        """Mark the device present, bridge it if enrolled, and resume its session."""
        self._presence.session_opened(identity.client_id, peer_address)
        if not self._is_bridgeable(identity.client_id):
            return
        self._devices.ensure_device(identity)
        # Reconcile immediately rather than waiting for the next supervisor
        # tick, so a returning feeder is back on the cloud without delay.
        self._devices.sync_upstream_sessions()
        bridge = self._bridge_holder.bridge
        if bridge is not None:
            bridge.forget_unknown_device(identity.client_id)

    def device_session_closed(self, client_id: str) -> None:
        """Mark the device absent and let the grace period run.

        The cloud session is deliberately *not* closed here. A feeder
        reconnects constantly, and dropping the upstream on every blip would
        churn sessions for no reason. The supervisor closes it only once the
        device has stayed away past the presence grace.
        """
        self._presence.session_closed(client_id)

    def _is_bridgeable(self, client_id: str) -> bool:
        return any(entry.client_id == client_id for entry in self._registry.get_bridgeable())


class BridgeHolder:
    """Lets the session listener reach a bridge that is created after it."""

    def __init__(self) -> None:
        self.bridge: MqttBridge | None = None


def _install_shutdown_handler(stop_event: threading.Event) -> None:
    def _handle_shutdown_signal(signal_number: int, frame: FrameType | None) -> None:
        _LOGGER.info("Received signal %d, shutting down", signal_number)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)


def _seed_manual_identity(config: RelayConfig, registry: DeviceRegistry) -> None:
    """Record a hand-configured device so it is bridged before it connects.

    Optional, and additive: it enrolls that one device without limiting the
    relay to it.
    """
    manual = config.manually_configured_identity()
    if manual is None:
        return
    client_id, username, password = manual
    _LOGGER.info("Seeding manually configured device identity: client_id=%s", client_id)
    registry.record(DeviceIdentity(client_id=client_id, username=username, password=password))


def main() -> None:
    """Load configuration and run the relay until a shutdown signal is received."""
    config = RelayConfig.from_env()
    log_buffer = configure_logging(config.log_level)
    _LOGGER.info(
        "Starting petlibro-relay (upstream=%s:%d, local=%s:%d, capture-proxy=%s:%d, auto_enroll=%s)",
        config.upstream_host,
        config.upstream_port,
        config.local_host,
        config.local_port,
        config.capture_proxy_listen_host,
        config.capture_proxy_listen_port,
        config.auto_enroll,
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
        auto_enroll=config.auto_enroll,
    )
    registry.purge_expired()
    _seed_manual_identity(config, registry)

    presence = DevicePresenceTracker()
    devices = DeviceManager(config, registry, queue, shadow, state_cache, telemetry, presence)
    dashboard_context = DashboardContext(
        config, registry, queue, shadow, telemetry, log_buffer, devices, presence
    )

    dashboard_server: DashboardServer | None = None
    if config.web_enabled:
        dashboard_server = DashboardServer(dashboard_context, config.web_host, config.web_port)
        dashboard_server.start()

    bridge_holder = BridgeHolder()
    capture_proxy = CredentialCaptureProxy(
        listen_host=config.capture_proxy_listen_host,
        listen_port=config.capture_proxy_listen_port,
        broker_host=config.local_host,
        broker_port=config.local_port,
        registry=registry,
        listener=DeviceEnroller(registry, devices, presence, bridge_holder),
    )
    capture_proxy.start()

    # Register the local subscription before any feeder can connect, so the
    # broker holds its opening burst for us instead of dropping it while we
    # are still learning that device's identity from that same burst.
    prime_local_subscription(config)

    bridge = MqttBridge(config, devices, queue, telemetry)
    bridge_holder.bridge = bridge
    try:
        # Every already-enrolled device comes up here; anything learned later
        # is started by DeviceEnroller as soon as it connects.
        devices.start()
        bridge.run_forever()
        stop_event.wait()
    finally:
        # Ordered teardown: stop taking new feeder connections, stop routing,
        # then close each device's cloud session, then the web server, and
        # only then the databases everything else was reading.
        capture_proxy.stop()
        bridge.stop()
        devices.stop()
        if dashboard_server is not None:
            dashboard_server.stop()
        queue.close()
        registry.close()
        shadow.close()
        _LOGGER.info("Relay stopped")


if __name__ == "__main__":
    main()
