"""Configuration loading for the PETLIBRO MQTT relay."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_UPSTREAM_HOST = "mqtt.us.petlibro.com"
DEFAULT_UPSTREAM_PORT = 1883
DEFAULT_LOCAL_HOST = "localhost"
DEFAULT_LOCAL_PORT = 1883
DEFAULT_KEEPALIVE_SECONDS = 90
DEFAULT_STATE_CACHE_PATH = "/data/state_cache.json"
DEFAULT_QUEUE_DB_PATH = "/data/relay_queue.sqlite3"
DEFAULT_MAX_QUEUE_SIZE = 5000
DEFAULT_LOG_LEVEL = "INFO"


class MissingConfigError(RuntimeError):
    """Raised when a required environment variable is missing."""


@dataclass(frozen=True, slots=True)
class RelayConfig:
    """Runtime configuration for the MQTT relay, loaded from environment variables."""

    device_client_id: str
    device_username: str
    device_password: str
    topic_prefix: str
    upstream_host: str
    upstream_port: int
    local_host: str
    local_port: int
    keepalive_seconds: int
    state_cache_path: str
    queue_db_path: str
    max_queue_size: int
    log_level: str

    @classmethod
    def from_env(cls) -> "RelayConfig":
        """Build configuration from environment variables.

        Raises:
            MissingConfigError: If a required environment variable is not set.
        """
        device_client_id = _require_env("PETLIBRO_DEVICE_CLIENT_ID")
        return cls(
            device_client_id=device_client_id,
            device_username=_require_env("PETLIBRO_DEVICE_USERNAME"),
            device_password=_require_env("PETLIBRO_DEVICE_PASSWORD"),
            topic_prefix=os.environ.get("PETLIBRO_TOPIC_PREFIX", f"dl/PLAF203/{device_client_id}"),
            upstream_host=os.environ.get("PETLIBRO_UPSTREAM_HOST", DEFAULT_UPSTREAM_HOST),
            upstream_port=int(os.environ.get("PETLIBRO_UPSTREAM_PORT", DEFAULT_UPSTREAM_PORT)),
            local_host=os.environ.get("PETLIBRO_LOCAL_HOST", DEFAULT_LOCAL_HOST),
            local_port=int(os.environ.get("PETLIBRO_LOCAL_PORT", DEFAULT_LOCAL_PORT)),
            keepalive_seconds=int(
                os.environ.get("PETLIBRO_KEEPALIVE_SECONDS", DEFAULT_KEEPALIVE_SECONDS)
            ),
            state_cache_path=os.environ.get("PETLIBRO_STATE_CACHE_PATH", DEFAULT_STATE_CACHE_PATH),
            queue_db_path=os.environ.get("PETLIBRO_QUEUE_DB_PATH", DEFAULT_QUEUE_DB_PATH),
            max_queue_size=int(os.environ.get("PETLIBRO_MAX_QUEUE_SIZE", DEFAULT_MAX_QUEUE_SIZE)),
            log_level=os.environ.get("PETLIBRO_LOG_LEVEL", DEFAULT_LOG_LEVEL),
        )


def _require_env(name: str) -> str:
    """Read a required environment variable.

    Args:
        name: Environment variable name.

    Returns:
        The environment variable's value.

    Raises:
        MissingConfigError: If the variable is unset or empty.
    """
    value = os.environ.get(name)
    if not value:
        raise MissingConfigError(f"Missing required environment variable: {name}")
    return value
