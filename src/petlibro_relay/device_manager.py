"""Owns the set of devices this relay bridges, and their lifecycles.

One relay process, N devices. The manager is the only place that creates or
destroys a `DeviceContext`, which keeps two things true:

* a device learned at runtime is bridged without a restart or a redeploy;
* a message is routed by looking its device up here, never by assuming there
  is only one.

Devices come from two places, and both funnel through `ensure_device`: the
registry at startup (everything already enrolled) and the capture proxy at
runtime (whatever connects next).

## Upstream sessions follow local presence

A device's cloud session is only held while that device is actually here. A
feeder that is powered off must not have the relay connected to PETLIBRO in
its name - that would report the device online when it cannot answer anything.

Rather than reacting to individual events, a single supervisor thread
periodically reconciles every device's session against
`DevicePresenceTracker`. Both `start_upstream` and `stop_upstream` are
idempotent, so whatever order enrollment, presence and startup happen in, the
next tick converges on the correct state - which is what makes the boot path
race-free.
"""

from __future__ import annotations

import logging
import threading

from .config import RelayConfig
from .device_context import DeviceContext
from .device_presence import DevicePresenceTracker
from .device_registry import DeviceIdentity, DeviceRegistry
from .local_responder import LocalResponder
from .message_queue import MessageQueue
from .observability.telemetry import RelayTelemetry
from .state_cache import StateCache
from .state_shadow import StateShadow

_LOGGER = logging.getLogger(__name__)

# How often sessions are reconciled against local presence. Well under the
# presence grace, so a device that goes away is disconnected promptly without
# the supervisor being a busy loop.
SUPERVISOR_INTERVAL_SECONDS = 5.0
SUPERVISOR_JOIN_TIMEOUT_SECONDS = 10.0


class DeviceManager:
    """Creates, tracks and tears down one `DeviceContext` per device."""

    def __init__(
        self,
        config: RelayConfig,
        registry: DeviceRegistry,
        queue: MessageQueue,
        shadow: StateShadow,
        state_cache: StateCache,
        telemetry: RelayTelemetry,
        presence: DevicePresenceTracker,
        supervisor_interval_seconds: float = SUPERVISOR_INTERVAL_SECONDS,
    ) -> None:
        """Initialize the manager without starting any device.

        Args:
            config: Relay runtime configuration.
            registry: Store of learned identities and their enrollment status.
            queue: Shared durable queue, scoped per device on every access.
            shadow: Shared state shadow, scoped per device on every access.
            state_cache: Shared last-payload-per-topic cache.
            telemetry: Relay-wide telemetry, which vends per-device metrics.
            presence: Which devices currently hold a local session.
            supervisor_interval_seconds: How often sessions are reconciled
                against presence.
        """
        self._config = config
        self._registry = registry
        self._queue = queue
        self._shadow = shadow
        self._state_cache = state_cache
        self._telemetry = telemetry
        self._presence = presence
        self._supervisor_interval_seconds = supervisor_interval_seconds
        self._lock = threading.Lock()
        self._contexts: dict[str, DeviceContext] = {}
        self._started = False
        self._stop_event = threading.Event()
        self._supervisor = threading.Thread(
            target=self._supervise, name="device-supervisor", daemon=True
        )

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        """Bring up every already-enrolled device, then accept new ones.

        Called once at boot. Contexts are rebuilt for every enrolled device,
        but a cloud session is only opened for those actually present - a
        feeder that is powered off is restored ready, not connected on its
        behalf.

        A device can connect while this is running, or just before it. Neither
        is a race: `sync_upstream_sessions` reconciles whatever exists against
        presence, and the supervisor repeats that on every tick.
        """
        with self._lock:
            self._started = True
        identities = self._registry.get_bridgeable()
        if not identities:
            _LOGGER.info(
                "No enrolled devices yet - waiting for a feeder to connect locally so its "
                "identity can be learned from its own CONNECT packet"
            )
        for identity in identities:
            self.ensure_device(identity)
        if identities:
            _LOGGER.info("Restored %d enrolled device(s) from the registry", len(identities))
        self.sync_upstream_sessions()
        self._supervisor.start()

    def sync_upstream_sessions(self) -> None:
        """Align every device's cloud session with whether it is locally present.

        Idempotent and safe to call from anywhere. Each device is handled
        independently, so one device's transition never touches another's
        session.
        """
        with self._lock:
            if not self._started:
                return
        for context in self.list_devices():
            if self._presence.is_online(context.device_id):
                if context.start_upstream():
                    _LOGGER.info(
                        "Device %s is present locally - resuming its PETLIBRO session",
                        context.device_id,
                    )
            elif context.stop_upstream(reason="device not present locally"):
                _LOGGER.info(
                    "Device %s has been absent for more than %.0fs - closing its PETLIBRO "
                    "session rather than reporting it online",
                    context.device_id,
                    self._presence.grace_seconds,
                )

    def _supervise(self) -> None:
        """Reconcile sessions against presence until shutdown."""
        while not self._stop_event.wait(self._supervisor_interval_seconds):
            self.sync_upstream_sessions()

    def ensure_device(self, identity: DeviceIdentity) -> DeviceContext:
        """Return this device's context, creating and starting it if new.

        Idempotent: a device reconnecting - which happens constantly - must
        reuse its existing context and its existing cloud session rather than
        opening a second one.

        Args:
            identity: The device's credentials and product.

        Returns:
            The device's context, running if the manager has been started.
        """
        with self._lock:
            existing = self._contexts.get(identity.client_id)
            if existing is not None:
                return existing
            context = DeviceContext(
                identity=identity,
                config=self._config,
                queue=self._queue,
                shadow=self._shadow,
                state_cache=self._state_cache,
                telemetry=self._telemetry.device(identity.client_id),
                responder=self._build_responder(),
            )
            self._contexts[identity.client_id] = context
            started = self._started
        _LOGGER.info(
            "Device %s (product=%s) is now bridged by this relay",
            identity.client_id,
            identity.product_id,
        )
        self._telemetry.record_event(
            "device_added",
            f"Device {identity.client_id} bridged",
            device_id=identity.client_id,
            details={"product_id": identity.product_id},
        )
        if started and self._presence.is_online(identity.client_id):
            context.start_upstream()
        return context

    def _build_responder(self) -> LocalResponder:
        """Give each device an observer, even when local replies are disabled.

        `LocalResponder.enabled` gates answering, not learning: the shadow
        must keep receiving cloud settings and schedules in pure-pipe mode.
        """
        return LocalResponder(
            self._config.local_responder, self._shadow, self._config.handled_msg_id_ttl_seconds
        )

    def remove_device(self, device_id: str) -> None:
        """Stop bridging one device, leaving every other untouched."""
        with self._lock:
            context = self._contexts.pop(device_id, None)
        if context is None:
            return
        context.stop_upstream(reason="device removed")
        self._telemetry.forget_device(device_id)
        _LOGGER.info("Device %s is no longer bridged", device_id)

    def stop(self) -> None:
        """Stop every device's cloud session, leaving no Paho thread behind.

        The supervisor is stopped first, so it cannot resurrect a session
        while the rest of the shutdown is running.
        """
        self._stop_event.set()
        if self._supervisor.is_alive():
            self._supervisor.join(timeout=SUPERVISOR_JOIN_TIMEOUT_SECONDS)
        with self._lock:
            contexts = list(self._contexts.values())
            self._contexts.clear()
            self._started = False
        for context in contexts:
            context.stop_upstream(reason="relay shutdown")

    # -- lookups -----------------------------------------------------------------

    def get_by_device_id(self, device_id: str) -> DeviceContext | None:
        """Return the context for a device id, or `None` if it is not bridged."""
        with self._lock:
            return self._contexts.get(device_id)

    def get_by_client_id(self, client_id: str) -> DeviceContext | None:
        """Return the context for a MQTT client id.

        The feeder uses the same value as its client id and in its topics, so
        this is an alias kept for call sites that hold one rather than the
        other.
        """
        return self.get_by_device_id(client_id)

    def list_devices(self) -> list[DeviceContext]:
        """Return every bridged device, ordered for stable rendering."""
        with self._lock:
            contexts = list(self._contexts.values())
        return sorted(contexts, key=lambda context: context.device_id)

    def device_ids(self) -> list[str]:
        """Return the ids of every bridged device."""
        return [context.device_id for context in self.list_devices()]
