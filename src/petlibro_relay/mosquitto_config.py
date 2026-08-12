"""Renders the local mosquitto broker's config file from environment variables.

Mosquitto has no runtime API for broker-level settings (listener port,
persistence, queueing policy) - those can only be set via its config file or
a small number of command-line flags, and none of that covers the settings
this relay needs (`queue_qos0_messages`, `persistent_client_expiration`,
`max_queued_messages`). Rather than bake a templating script into a custom
mosquitto image, this relay owns config generation: it reads the same
environment-variable-driven model as the rest of the relay and writes the
rendered file to a volume that the stock, unmodified mosquitto image mounts
read-only (see the "mosquitto-config" one-shot service in docker-compose.yml).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from string import Template

_LOGGER = logging.getLogger(__name__)

DEFAULT_OUTPUT_PATH = "/mosquitto-config/mosquitto.conf"
DEFAULT_LISTENER_PORT = 1883
DEFAULT_ALLOW_ANONYMOUS = "true"
DEFAULT_PERSISTENT_CLIENT_EXPIRATION = "7d"
DEFAULT_QUEUE_QOS0_MESSAGES = "true"
DEFAULT_MAX_QUEUED_MESSAGES = 1000

TEMPLATE_PATH = Path(__file__).parent / "templates" / "mosquitto.conf.template"


@dataclass(frozen=True, slots=True)
class MosquittoConfig:
    """Environment-driven settings for the local mosquitto broker."""

    listener_port: int
    allow_anonymous: str
    persistent_client_expiration: str
    queue_qos0_messages: str
    max_queued_messages: int
    output_path: str

    @classmethod
    def from_env(cls) -> "MosquittoConfig":
        """Build settings from environment variables, defaulting anything unset."""
        return cls(
            listener_port=int(os.environ.get("MOSQUITTO_LISTENER_PORT", DEFAULT_LISTENER_PORT)),
            allow_anonymous=os.environ.get("MOSQUITTO_ALLOW_ANONYMOUS", DEFAULT_ALLOW_ANONYMOUS),
            persistent_client_expiration=os.environ.get(
                "MOSQUITTO_PERSISTENT_CLIENT_EXPIRATION", DEFAULT_PERSISTENT_CLIENT_EXPIRATION
            ),
            queue_qos0_messages=os.environ.get(
                "MOSQUITTO_QUEUE_QOS0_MESSAGES", DEFAULT_QUEUE_QOS0_MESSAGES
            ),
            max_queued_messages=int(
                os.environ.get("MOSQUITTO_MAX_QUEUED_MESSAGES", DEFAULT_MAX_QUEUED_MESSAGES)
            ),
            output_path=os.environ.get("MOSQUITTO_CONFIG_OUTPUT_PATH", DEFAULT_OUTPUT_PATH),
        )


def render(config: MosquittoConfig) -> str:
    """Render the mosquitto.conf content for the given settings.

    Args:
        config: Settings to substitute into the template.

    Returns:
        The rendered config file content.
    """
    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")
    return Template(template_text).substitute(
        MOSQUITTO_LISTENER_PORT=config.listener_port,
        MOSQUITTO_ALLOW_ANONYMOUS=config.allow_anonymous,
        MOSQUITTO_PERSISTENT_CLIENT_EXPIRATION=config.persistent_client_expiration,
        MOSQUITTO_QUEUE_QOS0_MESSAGES=config.queue_qos0_messages,
        MOSQUITTO_MAX_QUEUED_MESSAGES=config.max_queued_messages,
    )


def write(config: MosquittoConfig) -> None:
    """Render and write the mosquitto config file to `config.output_path`.

    Args:
        config: Settings to render and where to write them.
    """
    output_path = Path(config.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        output_path.write_text(render(config), encoding="utf-8")
    except OSError:
        _LOGGER.exception("Failed to write mosquitto config to %s", output_path)
        raise
    _LOGGER.info("Wrote mosquitto config to %s", output_path)


def main() -> None:
    """Entrypoint: render the mosquitto config file from the environment and exit."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    write(MosquittoConfig.from_env())


if __name__ == "__main__":
    main()
