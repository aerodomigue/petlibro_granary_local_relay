"""PETLIBRO MQTT command names and topic helpers.

Names and payload shapes here are grounded, in order of precedence, in:

1. Traffic captured from this project's own PLAF203 on firmware V3.0.30.
2. https://github.com/icex2/plaf203 (reverse-engineered PLAF203S, FW 3.0.14).
3. https://github.com/bobobo1618/catbro-server.

Where they disagree, the captured traffic wins - see `local_responder` for a
concrete case (this firmware's `NTP_SYNC` carries DST fields that icex2's
older firmware does not).
"""

from __future__ import annotations

from typing import Final

# Product identifier as it appears in topics. Uppercase: confirmed both in our
# own capture and in icex2's DEVICE_PRODUCT_ID.
PRODUCT_ID: Final = "PLAF203"

POST_SUFFIX: Final = "post"  # device -> cloud
SUB_SUFFIX: Final = "sub"  # cloud -> device


class Command:
    """Known `cmd` values."""

    # Time synchronisation. The device's request carries no msgId; the cloud
    # replies with one it generates itself.
    NTP: Final = "NTP"
    NTP_SYNC: Final = "NTP_SYNC"

    # Feeding plans. Request/response correlated by msgId: the device posts
    # the plan ids and syncTimes it holds, the cloud answers with the same
    # msgId and the full plan definitions.
    FEEDING_PLAN_SERVICE: Final = "FEEDING_PLAN_SERVICE"
    DEVICE_FEEDING_PLAN_SERVICE: Final = "DEVICE_FEEDING_PLAN_SERVICE"
    GET_FEEDING_PLAN_EVENT: Final = "GET_FEEDING_PLAN_EVENT"

    # Settings / configuration.
    ATTR_SET_SERVICE: Final = "ATTR_SET_SERVICE"
    ATTR_GET_SERVICE: Final = "ATTR_GET_SERVICE"
    ATTR_PUSH_EVENT: Final = "ATTR_PUSH_EVENT"
    GET_CONFIG: Final = "GET_CONFIG"
    SERVER_CONFIG_PUSH: Final = "SERVER_CONFIG_PUSH"
    DEVICE_CONFIG_SYNC: Final = "DEVICE_CONFIG_SYNC"
    DEVICE_PROPERTIES_SERVICE: Final = "DEVICE_PROPERTIES_SERVICE"
    DEVICE_INFO_SERVICE: Final = "DEVICE_INFO_SERVICE"

    # Device-reported state.
    HEARTBEAT: Final = "HEARTBEAT"
    DEVICE_START_EVENT: Final = "DEVICE_START_EVENT"
    DEVICE_LOG_REPORT_EVENT: Final = "DEVICE_LOG_REPORT_EVENT"
    GRAIN_OUTPUT_EVENT: Final = "GRAIN_OUTPUT_EVENT"
    ERROR_EVENT: Final = "ERROR_EVENT"

    # Physical actions - never answered locally.
    MANUAL_FEEDING_SERVICE: Final = "MANUAL_FEEDING_SERVICE"
    DEVICE_REBOOT: Final = "DEVICE_REBOOT"
    RESET: Final = "RESET"
    RESTORE: Final = "RESTORE"


def topic_prefix(device_id: str) -> str:
    """Return the topic prefix for a device."""
    return f"dl/{PRODUCT_ID}/{device_id}"


def sub_topic(device_id: str, category: str) -> str:
    """Return the cloud -> device topic for a category (e.g. "ntp")."""
    return f"{topic_prefix(device_id)}/device/{category}/{SUB_SUFFIX}"


def category_of(topic: str) -> str | None:
    """Return the category segment of a device topic, or None if it isn't one.

    Args:
        topic: A full MQTT topic, e.g. `dl/PLAF203/<id>/device/ntp/post`.
    """
    parts = topic.split("/")
    if len(parts) != 6 or parts[0] != "dl" or parts[3] != "device":
        return None
    return parts[4]


def is_post(topic: str) -> bool:
    """Return True if the topic is a device -> cloud ("/post") topic."""
    return topic.endswith(f"/{POST_SUFFIX}")
