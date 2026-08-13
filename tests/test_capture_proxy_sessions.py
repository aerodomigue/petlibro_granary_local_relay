"""Tests that the capture proxy tracks many devices' sessions independently.

The proxy is the only source of truth for "connected right now", and it serves
every feeder at once. It must therefore never hold a single current identity
that one device could overwrite for another, and must not report a device
offline while a reconnect is still overlapping its predecessor.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from petlibro_relay.credential_capture_proxy import CredentialCaptureProxy, DeviceSessionListener
from petlibro_relay.device_registry import DeviceIdentity, DeviceRegistry

DEVICE_A = DeviceIdentity(client_id="DEVICE-A", username="user-a", password="pass-a")
DEVICE_B = DeviceIdentity(client_id="DEVICE-B", username="user-b", password="pass-b")
DEVICE_C = DeviceIdentity(client_id="DEVICE-C", username="user-c", password="pass-c")


class RecordingListener(DeviceSessionListener):
    """Captures session callbacks so ordering and pairing can be asserted."""

    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self._lock = threading.Lock()

    def device_session_opened(self, identity: DeviceIdentity, peer_address: str) -> None:
        """Record an opened session."""
        with self._lock:
            self.opened.append((identity.client_id, peer_address))

    def device_session_closed(self, client_id: str) -> None:
        """Record a closed session."""
        with self._lock:
            self.closed.append(client_id)


@pytest.fixture
def proxy(tmp_path: Path) -> Iterator[tuple[CredentialCaptureProxy, RecordingListener]]:
    """A proxy wired to a throwaway registry. No sockets are ever opened."""
    registry = DeviceRegistry(str(tmp_path / "registry.sqlite3"))
    listener = RecordingListener()
    instance = CredentialCaptureProxy(
        listen_host="127.0.0.1",
        listen_port=0,
        broker_host="127.0.0.1",
        broker_port=0,
        registry=registry,
        listener=listener,
    )
    yield instance, listener
    registry.close()


def test_three_devices_hold_independent_sessions(
    proxy: tuple[CredentialCaptureProxy, RecordingListener],
) -> None:
    """No single "current identity" exists that one device could clobber."""
    instance, listener = proxy

    for index, identity in enumerate((DEVICE_A, DEVICE_B, DEVICE_C)):
        instance._open_session(identity, f"10.3.100.{index}")

    assert instance.connected_device_ids() == {"DEVICE-A", "DEVICE-B", "DEVICE-C"}
    assert [client_id for client_id, _ in listener.opened] == [
        "DEVICE-A",
        "DEVICE-B",
        "DEVICE-C",
    ]


def test_one_device_disconnecting_leaves_the_others_connected(
    proxy: tuple[CredentialCaptureProxy, RecordingListener],
) -> None:
    """Closing B's session must not affect A or C."""
    instance, listener = proxy
    for identity in (DEVICE_A, DEVICE_B, DEVICE_C):
        instance._open_session(identity, "10.3.100.1")

    instance._close_session("DEVICE-B")

    assert instance.connected_device_ids() == {"DEVICE-A", "DEVICE-C"}
    assert listener.closed == ["DEVICE-B"]


def test_overlapping_reconnect_is_not_reported_as_a_disconnect(
    proxy: tuple[CredentialCaptureProxy, RecordingListener],
) -> None:
    """A feeder opening its new link before dropping the old stays online."""
    instance, listener = proxy
    instance._open_session(DEVICE_A, "10.3.100.1")

    instance._open_session(DEVICE_A, "10.3.100.1")  # reconnect, overlapping
    instance._close_session("DEVICE-A")  # the original link finally drops

    assert instance.is_device_connected("DEVICE-A") is True
    assert listener.closed == [], "an overlapping reconnect must not read as offline"

    instance._close_session("DEVICE-A")
    assert instance.is_device_connected("DEVICE-A") is False
    assert listener.closed == ["DEVICE-A"]


def test_concurrent_connects_do_not_lose_sessions(
    proxy: tuple[CredentialCaptureProxy, RecordingListener],
) -> None:
    """Session accounting holds under simultaneous connects and disconnects."""
    instance, _ = proxy
    rounds = 25

    def churn(identity: DeviceIdentity) -> None:
        for _ in range(rounds):
            instance._open_session(identity, "10.3.100.1")
            instance._close_session(identity.client_id)

    threads = [
        threading.Thread(target=churn, args=(identity,))
        for identity in (DEVICE_A, DEVICE_B, DEVICE_C)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert instance.connected_device_ids() == set(), "every session must be accounted for"


def test_each_device_keeps_its_own_source_address(
    proxy: tuple[CredentialCaptureProxy, RecordingListener],
) -> None:
    """The reported address belongs to the device that connected from it."""
    instance, listener = proxy

    instance._open_session(DEVICE_A, "10.3.100.90")
    instance._open_session(DEVICE_B, "10.3.100.91")

    assert dict(listener.opened) == {"DEVICE-A": "10.3.100.90", "DEVICE-B": "10.3.100.91"}
