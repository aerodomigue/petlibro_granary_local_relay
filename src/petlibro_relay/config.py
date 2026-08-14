"""Configuration loading for the PETLIBRO MQTT relay."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .local_responder import LocalResponderSettings

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
DEFAULT_AUTO_ENROLL = True
DEFAULT_STATE_SHADOW_DB_PATH = "/data/state_shadow.sqlite3"
DEFAULT_DEVICE_TIMEZONE = "UTC"
DEFAULT_HANDLED_MSG_ID_TTL_SECONDS = 120.0
DEFAULT_CLOCK_DRIFT_TOLERANCE_SECONDS = 10.0
DEFAULT_MAX_QUEUE_SIZE = 5000
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_WEB_ENABLED = False
DEFAULT_WEB_HOST = "0.0.0.0"
DEFAULT_WEB_PORT = 8080
DEFAULT_REPLAY_RATE_PER_DEVICE = 5.0
DEFAULT_REPLAY_RATE_GLOBAL = 20.0
DEFAULT_REPLAY_START_DELAY_SECONDS = 1.5
DEFAULT_REPLAY_JITTER = 0.15
DEFAULT_LOG_UPSTREAM_SERVICE_PAYLOADS = False
DEFAULT_LOG_DEVICE_START_EVENT = False
DEFAULT_GO2RTC_ENABLED = False
DEFAULT_GO2RTC_HOST = "go2rtc"
DEFAULT_GO2RTC_PORT = 1984
DEFAULT_GO2RTC_TIMEOUT_SECONDS = 1.0
DEFAULT_CAMERA_BRIDGE_ENABLED = False
DEFAULT_CAMERA_BRIDGE_HOST = "camera-bridge"
DEFAULT_CAMERA_BRIDGE_PORT = 8081
DEFAULT_CAMERA_BRIDGE_TIMEOUT_SECONDS = 1.0

_LOOPBACK_UPSTREAM_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", "::"})
UNSAFE_UPSTREAM_CONFIGURATION_MESSAGE = (
    "Unsafe upstream configuration: upstream would connect to the local capture proxy / broker "
    "and create an MQTT loop."
)


@dataclass(frozen=True, slots=True)
class Go2RtcSettings:
    """Read-only connection settings for the optional go2rtc sidecar."""

    enabled: bool = DEFAULT_GO2RTC_ENABLED
    host: str = DEFAULT_GO2RTC_HOST
    port: int = DEFAULT_GO2RTC_PORT
    timeout_seconds: float = DEFAULT_GO2RTC_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class CameraBridgeSettings:
    """Connection settings for the internal PLAF203 camera-bridge sidecar."""

    enabled: bool = DEFAULT_CAMERA_BRIDGE_ENABLED
    host: str = DEFAULT_CAMERA_BRIDGE_HOST
    port: int = DEFAULT_CAMERA_BRIDGE_PORT
    timeout_seconds: float = DEFAULT_CAMERA_BRIDGE_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class RelayConfig:
    """Runtime configuration for the MQTT relay, loaded from environment variables.

    Device identities are not configured here as a rule: the relay learns each
    one automatically from that feeder's own CONNECT packet via
    `CredentialCaptureProxy` and `DeviceRegistry`, and bridges as many devices
    as connect. Adding a feeder therefore needs no configuration change and no
    second container.

    The `device_client_id` / `_username` / `_password` triplet remains as a
    manual seed for a single device, useful to bridge one before it has ever
    connected locally. It adds that device; it does not restrict the relay to
    it.
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
    auto_enroll: bool
    state_shadow_db_path: str
    handled_msg_id_ttl_seconds: float
    local_responder: LocalResponderSettings
    web_enabled: bool
    web_host: str
    web_port: int
    max_queue_size: int
    log_level: str
    replay_rate_per_device: float
    replay_rate_global: float
    replay_start_delay_seconds: float
    replay_jitter: float
    log_upstream_service_payloads: bool
    log_device_start_event: bool
    go2rtc: Go2RtcSettings
    camera_bridge: CameraBridgeSettings

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
            auto_enroll=_env_flag("PETLIBRO_AUTO_ENROLL", DEFAULT_AUTO_ENROLL),
            state_shadow_db_path=os.environ.get(
                "PETLIBRO_STATE_SHADOW_DB_PATH", DEFAULT_STATE_SHADOW_DB_PATH
            ),
            handled_msg_id_ttl_seconds=float(
                os.environ.get(
                    "PETLIBRO_HANDLED_MSG_ID_TTL_SECONDS", DEFAULT_HANDLED_MSG_ID_TTL_SECONDS
                )
            ),
            local_responder=_local_responder_from_env(),
            web_enabled=_env_flag("PETLIBRO_WEB_ENABLED", DEFAULT_WEB_ENABLED),
            web_host=os.environ.get("PETLIBRO_WEB_HOST", DEFAULT_WEB_HOST),
            web_port=int(os.environ.get("PETLIBRO_WEB_PORT", DEFAULT_WEB_PORT)),
            max_queue_size=int(os.environ.get("PETLIBRO_MAX_QUEUE_SIZE", DEFAULT_MAX_QUEUE_SIZE)),
            log_level=os.environ.get("PETLIBRO_LOG_LEVEL", DEFAULT_LOG_LEVEL),
            replay_rate_per_device=float(
                os.environ.get("PETLIBRO_REPLAY_RATE_PER_DEVICE", DEFAULT_REPLAY_RATE_PER_DEVICE)
            ),
            replay_rate_global=float(
                os.environ.get("PETLIBRO_REPLAY_RATE_GLOBAL", DEFAULT_REPLAY_RATE_GLOBAL)
            ),
            replay_start_delay_seconds=float(
                os.environ.get(
                    "PETLIBRO_REPLAY_START_DELAY", DEFAULT_REPLAY_START_DELAY_SECONDS
                )
            ),
            replay_jitter=float(os.environ.get("PETLIBRO_REPLAY_JITTER", DEFAULT_REPLAY_JITTER)),
            log_upstream_service_payloads=_env_flag(
                "PETLIBRO_LOG_UPSTREAM_SERVICE_PAYLOADS", DEFAULT_LOG_UPSTREAM_SERVICE_PAYLOADS
            ),
            log_device_start_event=_env_flag(
                "PETLIBRO_LOG_DEVICE_START_EVENT", DEFAULT_LOG_DEVICE_START_EVENT
            ),
            go2rtc=Go2RtcSettings(
                enabled=_env_flag("PETLIBRO_GO2RTC_ENABLED", DEFAULT_GO2RTC_ENABLED),
                host=os.environ.get("PETLIBRO_GO2RTC_HOST", DEFAULT_GO2RTC_HOST),
                port=int(os.environ.get("PETLIBRO_GO2RTC_PORT", DEFAULT_GO2RTC_PORT)),
                timeout_seconds=float(
                    os.environ.get("PETLIBRO_GO2RTC_TIMEOUT_SECONDS", DEFAULT_GO2RTC_TIMEOUT_SECONDS)
                ),
            ),
            camera_bridge=CameraBridgeSettings(
                enabled=_env_flag("PETLIBRO_CAMERA_BRIDGE_ENABLED", DEFAULT_CAMERA_BRIDGE_ENABLED),
                host=os.environ.get("PETLIBRO_CAMERA_BRIDGE_HOST", DEFAULT_CAMERA_BRIDGE_HOST),
                port=int(
                    os.environ.get("PETLIBRO_CAMERA_BRIDGE_PORT", DEFAULT_CAMERA_BRIDGE_PORT)
                ),
                timeout_seconds=float(
                    os.environ.get(
                        "PETLIBRO_CAMERA_BRIDGE_TIMEOUT_SECONDS",
                        DEFAULT_CAMERA_BRIDGE_TIMEOUT_SECONDS,
                    )
                ),
            ),
        )

    def manually_configured_identity(self) -> tuple[str, str, str] | None:
        """Return the manually-configured (client_id, username, password), if fully set."""
        if self.device_client_id and self.device_username and self.device_password:
            return self.device_client_id, self.device_username, self.device_password
        return None

    def validate_upstream_safety(self) -> None:
        """Reject an upstream endpoint that would point back into this relay.

        This check intentionally compares literal host values only. Resolving a
        hostname here would make startup dependent on external DNS and could
        turn an otherwise safe configuration into a false positive.

        Raises:
            ValueError: If a loopback/wildcard endpoint uses one of this
                relay's local MQTT ports.
        """
        upstream_host = self.upstream_host.strip().lower().rstrip(".")
        local_ports = {self.local_port, self.capture_proxy_listen_port}
        if upstream_host in _LOOPBACK_UPSTREAM_HOSTS and self.upstream_port in local_ports:
            raise ValueError(UNSAFE_UPSTREAM_CONFIGURATION_MESSAGE)

    def validate_startup_configuration(self) -> None:
        """Validate configuration before any relay component is constructed.

        Raises:
            ValueError: If the upstream target would loop locally or replay
                parameters are outside their safe operating range.
        """
        self.validate_upstream_safety()
        if self.replay_rate_per_device <= 0 or self.replay_rate_global <= 0:
            raise ValueError("Replay rates must be greater than zero")
        if self.replay_start_delay_seconds < 0:
            raise ValueError("Replay start delay must not be negative")
        if not 0 <= self.replay_jitter <= 1:
            raise ValueError("Replay jitter must be between zero and one")
        if self.go2rtc.port <= 0 or self.go2rtc.port > 65535:
            raise ValueError("go2rtc port must be between 1 and 65535")
        if self.go2rtc.timeout_seconds <= 0:
            raise ValueError("go2rtc timeout must be greater than zero")
        if self.camera_bridge.port <= 0 or self.camera_bridge.port > 65535:
            raise ValueError("camera bridge port must be between 1 and 65535")
        if self.camera_bridge.timeout_seconds <= 0:
            raise ValueError("camera bridge timeout must be greater than zero")


def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable ("1"/"true"/"yes"/"on" are true)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _local_responder_from_env() -> LocalResponderSettings:
    """Build local-responder feature flags. Everything defaults to off.

    The relay answering on the cloud's behalf changes what the device is told,
    so it stays opt-in and is enabled deliberately, per function. The master
    switch gates the per-function ones, so turning it off disables the whole
    fallback regardless of the rest.
    """
    enabled = _env_flag("PETLIBRO_LOCAL_RESPONDER")
    return LocalResponderSettings(
        enabled=enabled,
        ntp=enabled and _env_flag("PETLIBRO_LOCAL_NTP"),
        config=enabled and _env_flag("PETLIBRO_LOCAL_CONFIG"),
        feeding_plan=enabled and _env_flag("PETLIBRO_LOCAL_FEEDING_PLAN"),
        always_answer_ntp_locally=_env_flag("PETLIBRO_LOCAL_NTP_ALWAYS"),
        device_timezone=os.environ.get("PETLIBRO_DEVICE_TIMEZONE", DEFAULT_DEVICE_TIMEZONE),
        clock_drift_tolerance_seconds=float(
            os.environ.get(
                "PETLIBRO_CLOCK_DRIFT_TOLERANCE_SECONDS", DEFAULT_CLOCK_DRIFT_TOLERANCE_SECONDS
            )
        ),
    )
