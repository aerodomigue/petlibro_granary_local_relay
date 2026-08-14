"""Unit coverage for the read-only go2rtc camera-status POC."""

from __future__ import annotations

import io
import json
from typing import Any
from urllib.error import URLError

import pytest

from petlibro_relay.camera import Go2RtcCameraClient, stream_name_for_device
from petlibro_relay.config import Go2RtcSettings

DEVICE_A = "TESTDEVICE0000000001"
DEVICE_B = "TESTDEVICE0000000002"


class FakeHttpResponse:
    """Minimal context-managed HTTP response for stdlib JSON loading."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = io.BytesIO(json.dumps(payload).encode())

    def read(self, size: int = -1) -> bytes:
        """Return encoded JSON data."""
        return self._body.read(size)

    def __enter__(self) -> "FakeHttpResponse":
        """Enter the context manager."""
        return self

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None:
        """Close nothing; the in-memory response owns no resource."""


def _client() -> Go2RtcCameraClient:
    return Go2RtcCameraClient(Go2RtcSettings(enabled=True, host="go2rtc", port=1984))


def test_stream_names_are_deterministic_and_device_scoped() -> None:
    """Two devices must never map to one go2rtc stream name."""
    assert stream_name_for_device(DEVICE_A) == f"plaf203_{DEVICE_A}"
    assert stream_name_for_device(DEVICE_A) != stream_name_for_device(DEVICE_B)


def test_disabled_go2rtc_does_not_make_an_http_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled status checks stay inert and report no source configuration."""
    def unexpected_request(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("disabled go2rtc must not make an HTTP request")

    monkeypatch.setattr("petlibro_relay.camera.urlopen", unexpected_request)

    status = Go2RtcCameraClient(Go2RtcSettings(enabled=False)).status(DEVICE_A, "PLAF203")

    assert status.reason == "go2rtc_disabled"
    assert status.go2rtc_reachable is False
    assert status.configured is False


def test_unreachable_go2rtc_is_reported_without_leaking_connection_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network errors become a stable, non-sensitive diagnostic reason."""
    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise URLError("private-token-must-not-leak")

    monkeypatch.setattr("petlibro_relay.camera.urlopen", unavailable)

    status = _client().status(DEVICE_A, "PLAF203")

    assert status.reason == "go2rtc_unreachable"
    assert "private-token" not in str(status.snapshot())


def test_absent_stream_reports_reachable_but_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty go2rtc stream list is not a usable PLAF203 source."""
    monkeypatch.setattr(
        "petlibro_relay.camera.urlopen", lambda *_args, **_kwargs: FakeHttpResponse({})
    )

    status = _client().status(DEVICE_A, "PLAF203")

    assert status.go2rtc_reachable is True
    assert status.configured is False
    assert status.online is False
    assert status.available is False
    assert status.webrtc is False
    assert status.reason == "plaf203_tutk_unsupported"


def test_existing_online_stream_is_visible_but_not_exposed_as_a_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manual stream is observable without treating its source as supported."""
    stream = stream_name_for_device(DEVICE_A)
    monkeypatch.setattr(
        "petlibro_relay.camera.urlopen",
        lambda *_args, **_kwargs: FakeHttpResponse({stream: {"producers": [{"id": "safe"}]}}),
    )

    status = _client().status(DEVICE_A, "PLAF203")

    assert status.configured is True
    assert status.online is True
    assert status.available is False
    assert status.webrtc is False
    assert status.reason == "plaf203_tutk_unsupported"
