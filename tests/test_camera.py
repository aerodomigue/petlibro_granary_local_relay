"""Unit coverage for the device-scoped go2rtc camera integration."""

from __future__ import annotations

import io
import json
from typing import Any
from urllib.error import URLError

import pytest

from petlibro_relay.camera import Go2RtcCameraClient, Go2RtcStreamClient, stream_name_for_device
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
    """An empty go2rtc stream list reports a recoverable registration gap."""
    monkeypatch.setattr(
        "petlibro_relay.camera.urlopen", lambda *_args, **_kwargs: FakeHttpResponse({})
    )

    status = _client().status(DEVICE_A, "PLAF203")

    assert status.go2rtc_reachable is True
    assert status.configured is False
    assert status.online is False
    assert status.available is False
    assert status.webrtc is False
    assert status.reason == "stream_not_registered"


def test_existing_online_stream_is_exposed_as_a_device_scoped_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified PLAF203 stream is safe to expose through the relay WHEP proxy."""
    stream = stream_name_for_device(DEVICE_A)
    monkeypatch.setattr(
        "petlibro_relay.camera.urlopen",
        lambda *_args, **_kwargs: FakeHttpResponse({stream: {"producers": [{"id": "safe"}]}}),
    )

    status = _client().status(DEVICE_A, "PLAF203")

    assert status.configured is True
    assert status.online is True
    assert status.available is True
    assert status.webrtc is True
    assert status.player_available is True
    assert status.reason is None


def test_stream_registration_uses_only_device_scoped_internal_rtsp_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dynamic registration cannot select an arbitrary go2rtc source URL."""
    requests: list[object] = []
    responses = iter(
        (
            FakeHttpResponse({}),
            FakeHttpResponse({}),
            FakeHttpResponse({stream_name_for_device(DEVICE_A): {}}),
        )
    )

    def request(*args: object, **_kwargs: object) -> FakeHttpResponse:
        requests.append(args[0])
        return next(responses)

    monkeypatch.setattr("petlibro_relay.camera.urlopen", request)
    client = Go2RtcStreamClient(
        Go2RtcSettings(enabled=True, host="go2rtc", source_host="127.0.0.1", source_port=8554)
    )

    assert client.ensure_stream(DEVICE_A) is True
    assert len(requests) == 3
    assert "name=plaf203_TESTDEVICE0000000001" in requests[1].full_url
    assert "src=rtsp%3A%2F%2F127.0.0.1%3A8554%2Fdevice%2FTESTDEVICE0000000001" in requests[1].full_url
