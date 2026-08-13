"""Tests for strict topic parsing, which all multi-device routing depends on.

If a malformed topic parsed into a partially-valid address, the relay could
route one device's traffic onto another's session. Parsing therefore either
yields a complete address or nothing.
"""

from __future__ import annotations

import pytest

from petlibro_relay import protocol

DEVICE_ID = "TESTDEVICE0000000001"


def test_post_topic_parses_into_all_four_fields() -> None:
    """A device -> cloud topic yields product, device, category and direction."""
    address = protocol.parse_topic(f"dl/PLAF203/{DEVICE_ID}/device/heart/post")

    assert address is not None
    assert address.product_id == "PLAF203"
    assert address.device_id == DEVICE_ID
    assert address.category == "heart"
    assert address.is_post is True
    assert address.is_sub is False


def test_sub_topic_is_recognised_as_cloud_to_device() -> None:
    """A cloud -> device topic is parsed with the opposite direction."""
    address = protocol.parse_topic(f"dl/PLAF203/{DEVICE_ID}/device/ntp/sub")

    assert address is not None
    assert address.is_sub is True
    assert address.prefix == f"dl/PLAF203/{DEVICE_ID}"


def test_a_second_product_is_parsed_from_the_topic_not_assumed() -> None:
    """The product comes from the topic, so a non-PLAF203 device still routes."""
    address = protocol.parse_topic("dl/PLAF108/OTHER-DEVICE/device/event/post")

    assert address is not None
    assert address.product_id == "PLAF108"
    assert address.device_id == "OTHER-DEVICE"


@pytest.mark.parametrize(
    "topic",
    [
        "",
        "dl/PLAF203",
        f"dl/PLAF203/{DEVICE_ID}/device/heart",
        f"dl/PLAF203/{DEVICE_ID}/device/heart/post/extra",
        f"xx/PLAF203/{DEVICE_ID}/device/heart/post",
        f"dl/PLAF203/{DEVICE_ID}/gateway/heart/post",
        f"dl/PLAF203/{DEVICE_ID}/device/heart/publish",
        f"dl//{DEVICE_ID}/device/heart/post",
        "dl/PLAF203//device/heart/post",
        f"dl/PLAF203/{DEVICE_ID}/device//post",
    ],
)
def test_malformed_topics_are_rejected_outright(topic: str) -> None:
    """Anything not exactly the device shape parses to None, never partially."""
    assert protocol.parse_topic(topic) is None


def test_wildcards_are_not_mistaken_for_a_device() -> None:
    """The subscription filter itself must never resolve to a routable device."""
    address = protocol.parse_topic("dl/+/+/device/+/post")

    assert address is not None
    assert address.device_id == "+", (
        "a wildcard parses structurally but can never match a real device id, "
        "so lookups fall through to the unknown-device path"
    )


def test_topic_builders_round_trip() -> None:
    """A built topic parses back into the values it was built from."""
    topic = protocol.sub_topic(DEVICE_ID, "service", "PLAF203")

    address = protocol.parse_topic(topic)

    assert address is not None
    assert (address.device_id, address.category, address.product_id) == (
        DEVICE_ID,
        "service",
        "PLAF203",
    )


def test_category_of_uses_the_strict_parser() -> None:
    """`category_of` inherits the parser's strictness rather than splitting loosely."""
    assert protocol.category_of(f"dl/PLAF203/{DEVICE_ID}/device/config/post") == "config"
    assert protocol.category_of("nonsense") is None
