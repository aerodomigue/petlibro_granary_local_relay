"""Validation of the camera UID shape observed in PLAF203 device-start events."""

from __future__ import annotations

from typing import TypeGuard

CAMERA_UID_LENGTH = 20


def is_camera_uid(value: object) -> TypeGuard[str]:
    """Return whether a value has the verified printable 20-byte UID shape."""
    return isinstance(value, str) and len(value) == CAMERA_UID_LENGTH and all(
        0x21 <= ord(character) <= 0x7E for character in value
    )
