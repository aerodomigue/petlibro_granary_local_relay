"""Tests that the bridge only ever forwards traffic belonging to its own device.

The local subscription is deliberately device-agnostic (`dl/+/+/device/+/post`)
so it can be registered before any identity is known. That makes it the
bridge's job to discard anything published by a *different* device, rather
than forward it upstream over the active device's authenticated session.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from petlibro_relay.config import RelayConfig
from petlibro_relay.device_registry import DeviceIdentity
from petlibro_relay.local_responder import LocalResponderSettings
from petlibro_relay.message_queue import MessageQueue
from petlibro_relay.mqtt_bridge import LOCAL_TO_UPSTREAM, MqttBridge
from petlibro_relay.state_cache import StateCache

ACTIVE_DEVICE = DeviceIdentity(client_id="DEVICE-A", username="user-a", password="pass-a")
ACTIVE_TOPIC = "dl/PLAF203/DEVICE-A/device/event/post"
FOREIGN_TOPIC = "dl/PLAF203/DEVICE-B/device/event/post"
PAYLOAD = b'{"cmd":"GRAIN_OUTPUT_EVENT"}'


@dataclass
class FakeMessage:
    """Minimal stand-in for paho's MQTTMessage (only what the callback reads)."""

    topic: str
    payload: bytes
    qos: int = 0


@pytest.fixture
def bridge(tmp_path: Path) -> Iterator[tuple[MqttBridge, MessageQueue]]:
    """A bridge wired to throwaway storage. No sockets are opened."""
    config = RelayConfig(
        device_client_id=None,
        device_username=None,
        device_password=None,
        topic_prefix_override=None,
        upstream_host="unused.invalid",
        upstream_port=1883,
        local_host="unused.invalid",
        local_port=1883,
        capture_proxy_listen_host="127.0.0.1",
        capture_proxy_listen_port=1883,
        keepalive_seconds=90,
        state_cache_path=str(tmp_path / "state.json"),
        queue_db_path=str(tmp_path / "queue.sqlite3"),
        device_registry_db_path=str(tmp_path / "registry.sqlite3"),
        device_retention_hours=72,
        state_shadow_db_path=str(tmp_path / "shadow.sqlite3"),
        handled_msg_id_ttl_seconds=120.0,
        local_responder=LocalResponderSettings(),
        max_queue_size=100,
        log_level="INFO",
    )
    queue = MessageQueue(config.queue_db_path, config.max_queue_size)
    instance = MqttBridge(config, ACTIVE_DEVICE, StateCache(config.state_cache_path), queue)
    yield instance, queue
    queue.close()


def test_active_device_traffic_is_queued(bridge: tuple[MqttBridge, MessageQueue]) -> None:
    """The active device's own /post traffic is forwarded upstream."""
    instance, queue = bridge

    instance._on_local_message(None, None, FakeMessage(ACTIVE_TOPIC, PAYLOAD))

    assert queue.count(LOCAL_TO_UPSTREAM) == 1


def test_foreign_device_traffic_is_never_queued(bridge: tuple[MqttBridge, MessageQueue]) -> None:
    """Test 6: another device's traffic must not ride the active device's session."""
    instance, queue = bridge

    instance._on_local_message(None, None, FakeMessage(FOREIGN_TOPIC, PAYLOAD))

    assert queue.count(LOCAL_TO_UPSTREAM) == 0, (
        "a foreign device's message must never enter the active device's upstream queue"
    )


def test_prefix_lookalike_is_not_treated_as_the_active_device(
    bridge: tuple[MqttBridge, MessageQueue],
) -> None:
    """A client id that merely starts with the active one must not slip through."""
    instance, queue = bridge

    instance._on_local_message(
        None, None, FakeMessage("dl/PLAF203/DEVICE-A-EVIL/device/event/post", PAYLOAD)
    )

    assert queue.count(LOCAL_TO_UPSTREAM) == 0
