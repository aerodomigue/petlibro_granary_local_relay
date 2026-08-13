"""Safety checks for the intentionally narrow motion-detection diagnostic."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_probe_module() -> ModuleType:
    path = Path(__file__).parents[1] / "tools" / "motion_detection_probe.py"
    specification = importlib.util.spec_from_file_location("motion_detection_probe", path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def test_probe_payload_is_strictly_allowlisted() -> None:
    """The diagnostic can emit only the approved setting command and one field."""
    probe = _load_probe_module()

    payload = probe.build_motion_payload(True, "message-id", 123)

    assert payload == {
        "cmd": "ATTR_SET_SERVICE",
        "ts": 123,
        "msgId": "message-id",
        "motionDetectionSwitch": True,
    }


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("FALSE", False)])
def test_probe_accepts_only_explicit_boolean_values(raw: str, expected: bool) -> None:
    """The only controllable value is a JSON-style boolean switch state."""
    probe = _load_probe_module()

    assert probe.parse_bool(raw) is expected
    with pytest.raises(argparse.ArgumentTypeError):
        probe.parse_bool("1")


def test_probe_rejects_any_other_device() -> None:
    """The real probe cannot be pointed at a different device context."""
    probe = _load_probe_module()

    assert probe.main(["--device-id", "OTHER", "--original-value", "false"]) == 2
