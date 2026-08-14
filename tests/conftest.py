"""Shared fixtures for the relay test suite.

Building a `RelayConfig` by hand is verbose and every suite needs one, so it
lives here rather than being copied per module.

No test in this suite touches a real broker, a real device or the PETLIBRO
cloud: everything runs against throwaway SQLite files and fake MQTT clients.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from petlibro_relay.config import Go2RtcSettings, RelayConfig
from petlibro_relay.local_responder import LocalResponderSettings

RelayConfigFactory = Callable[..., RelayConfig]


@pytest.fixture
def make_config(tmp_path: Path) -> RelayConfigFactory:
    """Return a factory for a `RelayConfig` backed by throwaway storage.

    Keyword arguments override any field, so a test states only what it
    actually cares about.
    """

    def factory(**overrides: object) -> RelayConfig:
        defaults: dict[str, object] = {
            "device_client_id": None,
            "device_username": None,
            "device_password": None,
            "topic_prefix_override": None,
            "upstream_host": "unused.invalid",
            "upstream_port": 1883,
            "local_host": "unused.invalid",
            "local_port": 1883,
            "capture_proxy_listen_host": "127.0.0.1",
            "capture_proxy_listen_port": 1883,
            "keepalive_seconds": 90,
            "state_cache_path": str(tmp_path / "state.json"),
            "queue_db_path": str(tmp_path / "queue.sqlite3"),
            "device_registry_db_path": str(tmp_path / "registry.sqlite3"),
            "device_retention_hours": 72.0,
            "auto_enroll": True,
            "state_shadow_db_path": str(tmp_path / "shadow.sqlite3"),
            "handled_msg_id_ttl_seconds": 120.0,
            "local_responder": LocalResponderSettings(),
            "web_enabled": False,
            "web_host": "127.0.0.1",
            "web_port": 8080,
            "max_queue_size": 100,
            "log_level": "INFO",
            "replay_rate_per_device": 5.0,
            "replay_rate_global": 20.0,
            "replay_start_delay_seconds": 1.5,
            "replay_jitter": 0.15,
            "log_upstream_service_payloads": False,
            "log_device_start_event": False,
            "go2rtc": Go2RtcSettings(enabled=False),
        }
        defaults.update(overrides)
        return RelayConfig(**defaults)  # type: ignore[arg-type]

    return factory
