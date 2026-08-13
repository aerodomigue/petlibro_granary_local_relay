"""Transparent TCP proxy that learns a device's MQTT identity from its CONNECT packet.

Sits in front of the local mosquitto broker on the port the feeder actually
connects to. For each new connection: reads the CONNECT packet, records the
client ID / username / password into the `DeviceRegistry`, forwards those
exact bytes to mosquitto, then becomes a plain bidirectional byte pipe for
the rest of the session. Mosquitto never sees anything different - this
proxy only *observes* the handshake, it never terminates or re-originates
the MQTT session itself.

Every connection is handled on its own thread with no shared mutable identity,
so any number of feeders can connect, reconnect and overlap without one
overwriting another's capture.

The proxy is also the only component that knows whether a device is *actually
connected right now*, as opposed to merely known: it sees the TCP session open
and close. It reports both, counting concurrent sessions per device so a
reconnect that overlaps its predecessor does not read as a disconnect.
"""

from __future__ import annotations

import logging
import socket
import threading
from collections import Counter

from .device_registry import DeviceIdentity, DeviceRegistry
from .mqtt_connect_packet import MalformedConnectPacketError, read_connect_packet

_LOGGER = logging.getLogger(__name__)

RELAY_BUFFER_SIZE = 4096
LISTEN_BACKLOG = 8


class DeviceSessionListener:
    """Notified as devices open and close local sessions.

    Implemented by the wiring in `__main__`, which uses it to enroll devices
    the moment they are learned and to keep the dashboard's local-online view
    honest.
    """

    def device_session_opened(self, identity: DeviceIdentity, peer_address: str) -> None:
        """Called once a device's CONNECT has been parsed and recorded."""

    def device_session_closed(self, client_id: str) -> None:
        """Called when a device's last concurrent local session ends."""


class CredentialCaptureProxy:
    """Listens for the feeder's connection, captures its identity, then pipes it through."""

    def __init__(
        self,
        listen_host: str,
        listen_port: int,
        broker_host: str,
        broker_port: int,
        registry: DeviceRegistry,
        listener: DeviceSessionListener | None = None,
    ) -> None:
        """Initialize the proxy.

        Args:
            listen_host: Host to bind the public-facing listener on.
            listen_port: Port to bind the public-facing listener on (what the
                feeder connects to after the DNS override).
            broker_host: Hostname of the real local mosquitto broker.
            broker_port: Port of the real local mosquitto broker.
            registry: Store to persist learned device identities into.
            listener: Optional observer of device session lifecycle. It never
                affects proxying.
        """
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._broker_host = broker_host
        self._broker_port = broker_port
        self._registry = registry
        self._listener = listener
        self._server_socket: socket.socket | None = None
        self._stop_event = threading.Event()
        self._accept_thread = threading.Thread(target=self._accept_loop, name="capture-proxy-accept", daemon=True)
        self._session_lock = threading.Lock()
        self._open_sessions: Counter[str] = Counter()

    def is_device_connected(self, client_id: str) -> bool:
        """Return whether this device currently holds at least one local session."""
        with self._session_lock:
            return self._open_sessions[client_id] > 0

    def connected_device_ids(self) -> set[str]:
        """Return every device with a live local session."""
        with self._session_lock:
            return {client_id for client_id, count in self._open_sessions.items() if count > 0}

    def start(self) -> None:
        """Start listening and accepting connections in a background thread."""
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((self._listen_host, self._listen_port))
        server_socket.listen(LISTEN_BACKLOG)
        server_socket.settimeout(1.0)
        self._server_socket = server_socket
        _LOGGER.info(
            "Credential capture proxy listening on %s:%d, forwarding to %s:%d",
            self._listen_host,
            self._listen_port,
            self._broker_host,
            self._broker_port,
        )
        self._accept_thread.start()

    def stop(self) -> None:
        """Stop accepting new connections and close the listener."""
        self._stop_event.set()
        if self._server_socket is not None:
            self._server_socket.close()
        self._accept_thread.join(timeout=5.0)

    def _accept_loop(self) -> None:
        assert self._server_socket is not None
        while not self._stop_event.is_set():
            try:
                client_socket, client_address = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop_event.is_set():
                    return
                _LOGGER.exception("Error accepting a connection")
                continue
            _LOGGER.info("Feeder connection from %s", client_address)
            threading.Thread(
                target=self._handle_connection,
                args=(client_socket, client_address[0]),
                daemon=True,
            ).start()

    def _handle_connection(self, client_socket: socket.socket, peer_address: str) -> None:
        try:
            broker_socket = socket.create_connection((self._broker_host, self._broker_port))
        except OSError:
            _LOGGER.exception("Could not reach local broker %s:%d", self._broker_host, self._broker_port)
            client_socket.close()
            return

        client_id: str | None = None
        try:
            client_id = self._capture_and_forward_connect(
                client_socket, broker_socket, peer_address
            )
            self._pipe_bidirectionally(client_socket, broker_socket)
        finally:
            client_socket.close()
            broker_socket.close()
            if client_id is not None:
                self._close_session(client_id)

    def _capture_and_forward_connect(
        self, client_socket: socket.socket, broker_socket: socket.socket, peer_address: str
    ) -> str | None:
        """Record the device's identity, forward its CONNECT, open its session.

        Returns:
            The device's client id once a session has been opened for it, or
            `None` when nothing could be learned from this connection.
        """
        try:
            raw_packet, fields = read_connect_packet(client_socket)
        except MalformedConnectPacketError as error:
            _LOGGER.warning("Could not parse CONNECT packet (%s), forwarding raw bytes as-is", error)
            if error.raw_so_far:
                broker_socket.sendall(error.raw_so_far)
            return None

        broker_socket.sendall(raw_packet)

        if fields.username is None or fields.password is None:
            _LOGGER.warning(
                "CONNECT from client_id=%s has no username/password, nothing to learn", fields.client_id
            )
            return None

        identity = DeviceIdentity(
            client_id=fields.client_id, username=fields.username, password=fields.password
        )
        self._registry.record(identity)
        self._open_session(identity, peer_address)
        return identity.client_id

    def _open_session(self, identity: DeviceIdentity, peer_address: str) -> None:
        with self._session_lock:
            self._open_sessions[identity.client_id] += 1
        if self._listener is not None:
            self._listener.device_session_opened(identity, peer_address)

    def _close_session(self, client_id: str) -> None:
        """Report a device offline only when its last session is gone.

        A feeder reconnecting often opens the new connection before the old
        one is torn down, so counting concurrent sessions avoids reporting a
        device offline while it is in fact connected.
        """
        with self._session_lock:
            self._open_sessions[client_id] -= 1
            still_connected = self._open_sessions[client_id] > 0
            if not still_connected:
                del self._open_sessions[client_id]
        if still_connected:
            return
        _LOGGER.info("Device %s closed its last local session", client_id)
        if self._listener is not None:
            self._listener.device_session_closed(client_id)

    def _pipe_bidirectionally(self, socket_a: socket.socket, socket_b: socket.socket) -> None:
        thread_a_to_b = threading.Thread(target=self._pipe_one_direction, args=(socket_a, socket_b), daemon=True)
        thread_b_to_a = threading.Thread(target=self._pipe_one_direction, args=(socket_b, socket_a), daemon=True)
        thread_a_to_b.start()
        thread_b_to_a.start()
        thread_a_to_b.join()
        thread_b_to_a.join()

    def _pipe_one_direction(self, source: socket.socket, destination: socket.socket) -> None:
        try:
            while True:
                chunk = source.recv(RELAY_BUFFER_SIZE)
                if not chunk:
                    return
                destination.sendall(chunk)
        except OSError:
            return
