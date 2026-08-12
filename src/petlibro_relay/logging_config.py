"""Logging setup for the PETLIBRO MQTT relay."""

from __future__ import annotations

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(threadName)s %(name)s: %(message)s"


def configure_logging(level_name: str) -> None:
    """Configure the root logger to write structured lines to stdout.

    Args:
        level_name: Logging level name (e.g. "INFO", "DEBUG").
    """
    level = logging.getLevelName(level_name.upper())
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT, stream=sys.stdout)
