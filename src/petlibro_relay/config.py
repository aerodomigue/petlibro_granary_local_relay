"""Configuration loading for the PETLIBRO MQTT relay."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_UPSTREAM_HOST = "mqtt.us.petlibro.com"
DEFAULT_UPSTREAM_PORT = 1883
DEFAULT_LOCAL_HOST = "localhost"
DEFAULT_LOCAL_PORT = 1883
DEFAULT_CAPTURE_PROXY_LISTEN_HOST = "0.0.0.0"
DEFAULT_CAPTURE_PROXY_LISTEN_PORT = 1883
DEFAULT_KEEPALIVE_SECONDS = 90
DEFAULT_STATE_CACHE_PATH = "/data/state_cache.json"
DEFAULT_QUEUE_DB_PATH = "/data/relay_queue.sqlite3"
DEFAULT_DEVICE_REGISTRY_DB_PATH = "/data/device_registry.sqlite3"
DEFAULT_DEVICE_RETENTION_HOURS = 72
DEFAULT_MAX_QUEUE_SIZE = 5000
DEFAULT_LOG_LEVEL = "INFO"


@dataclass(frozen=True, slots=True)
class RelayConfig:
    """Runtime configuration for the MQTT relay, loaded from environment variables.

    The device's MQTT identity (`device_client_id` / `_username` / `_password`)
    is optional here: if unset, the relay learns it automatically from the
    feeder's own CONNECT packet via `CredentialCaptureProxy` and
    `DeviceRegistry`, rather than requiring it to be extracted and configured
    by hand. Setting all three still works, as a manual override (useful to
    run the relay before the feeder has ever connected locally).
    """

    device_client_id: str | None
    device_username: str | None
    device_password: str | None
    topic_prefix_override: str | None
    upstream_host: str
    upstream_port: int
    local_host: str
    local_port: int
    capture_proxy_listen_host: str
    capture_proxy_listen_port: int
    keepalive_seconds: int
    state_cache_path: str
    queue_db_path: str
    device_registry_db_path: str
    device_retention_hours: float
    max_queue_size: int
    log_level: str

    @classmethod
    def from_env(cls) -> "RelayConfig":
        """Build configuration from environment variables."""
        return cls(
            device_client_id=os.environ.get("PETLIBRO_DEVICE_CLIENT_ID") or None,
            device_username=os.environ.get("PETLIBRO_DEVICE_USERNAME") or None,
            device_password=os.environ.get("PETLIBRO_DEVICE_PASSWORD") or None,
            topic_prefix_override=os.environ.get("PETLIBRO_TOPIC_PREFIX") or None,
            upstream_host=os.environ.get("PETLIBRO_UPSTREAM_HOST", DEFAULT_UPSTREAM_HOST),
            upstream_port=int(os.environ.get("PETLIBRO_UPSTREAM_PORT", DEFAULT_UPSTREAM_PORT)),
            local_host=os.environ.get("PETLIBRO_LOCAL_HOST", DEFAULT_LOCAL_HOST),
            local_port=int(os.environ.get("PETLIBRO_LOCAL_PORT", DEFAULT_LOCAL_PORT)),
            capture_proxy_listen_host=os.environ.get(
                "PETLIBRO_CAPTURE_PROXY_HOST", DEFAULT_CAPTURE_PROXY_LISTEN_HOST
            ),
            capture_proxy_listen_port=int(
                os.environ.get("PETLIBRO_CAPTURE_PROXY_PORT", DEFAULT_CAPTURE_PROXY_LISTEN_PORT)
            ),
            keepalive_seconds=int(
                os.environ.get("PETLIBRO_KEEPALIVE_SECONDS", DEFAULT_KEEPALIVE_SECONDS)
            ),
            state_cache_path=os.environ.get("PETLIBRO_STATE_CACHE_PATH", DEFAULT_STATE_CACHE_PATH),
            queue_db_path=os.environ.get("PETLIBRO_QUEUE_DB_PATH", DEFAULT_QUEUE_DB_PATH),
            device_registry_db_path=os.environ.get(
                "PETLIBRO_DEVICE_REGISTRY_DB_PATH", DEFAULT_DEVICE_REGISTRY_DB_PATH
            ),
            device_retention_hours=float(
                os.environ.get("PETLIBRO_DEVICE_RETENTION_HOURS", DEFAULT_DEVICE_RETENTION_HOURS)
            ),
            max_queue_size=int(os.environ.get("PETLIBRO_MAX_QUEUE_SIZE", DEFAULT_MAX_QUEUE_SIZE)),
            log_level=os.environ.get("PETLIBRO_LOG_LEVEL", DEFAULT_LOG_LEVEL),
        )

    def manually_configured_identity(self) -> tuple[str, str, str] | None:
        """Return the manually-configured (client_id, username, password), if fully set."""
        if self.device_client_id and self.device_username and self.device_password:
            return self.device_client_id, self.device_username, self.device_password
        return None
