"""Logging setup for the PETLIBRO MQTT relay."""

from __future__ import annotations

import logging
import sys

from .observability.log_buffer import RingBufferLogHandler

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(threadName)s %(name)s: %(message)s"


def configure_logging(level_name: str) -> RingBufferLogHandler:
    """Configure stdout logging and return the dashboard's safe log buffer.

    Args:
        level_name: Logging level name (e.g. "INFO", "DEBUG").
    """
    level = logging.getLevelName(level_name.upper())
    if not isinstance(level, int):
        level = logging.INFO
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    formatter = logging.Formatter(LOG_FORMAT)
    if not root_logger.handlers:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)
    log_buffer = RingBufferLogHandler()
    log_buffer.setFormatter(formatter)
    root_logger.addHandler(log_buffer)
    return log_buffer
