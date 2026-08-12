"""Read-only API and SSE coverage for the observability dashboard."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from petlibro_relay.config import RelayConfig
from petlibro_relay.device_registry import DeviceIdentity, DeviceRegistry
from petlibro_relay.local_responder import LocalResponder, LocalResponderSettings
from petlibro_relay.message_queue import MessageQueue
from petlibro_relay.observability.log_buffer import RingBufferLogHandler
from petlibro_relay.observability.telemetry import RelayTelemetry
from petlibro_relay.state_shadow import StateShadow
from petlibro_relay.web.app import _stream_logs, create_app
from petlibro_relay.web.context import DashboardContext

DEVICE_ID = "TESTDEVICE0000000001"
USERNAME = "USER12345678"
PASSWORD = "must-not-appear"
CANDIDATE_PASSWORD = "candidate-must-not-appear"


@pytest.fixture
def dashboard(tmp_path: Path) -> Iterator[tuple[DashboardContext, RingBufferLogHandler]]:
    """Create a fully populated read-only dashboard context."""
    config = RelayConfig(
        device_client_id=None,
        device_username=None,
        device_password=None,
        topic_prefix_override=None,
        upstream_host="unused.invalid",
        upstream_port=1883,
        local_host="unused.invalid",
        local_port=1883,
        capture_proxy_listen_host="127.0.0.1",
        capture_proxy_listen_port=1883,
        keepalive_seconds=90,
        state_cache_path=str(tmp_path / "state.json"),
        queue_db_path=str(tmp_path / "queue.sqlite3"),
        device_registry_db_path=str(tmp_path / "registry.sqlite3"),
        device_retention_hours=72,
        state_shadow_db_path=str(tmp_path / "shadow.sqlite3"),
        handled_msg_id_ttl_seconds=120.0,
        local_responder=LocalResponderSettings(),
        web_enabled=True,
        web_host="127.0.0.1",
        web_port=8080,
        max_queue_size=100,
        log_level="INFO",
    )
    registry = DeviceRegistry(config.device_registry_db_path)
    identity = DeviceIdentity(DEVICE_ID, USERNAME, PASSWORD)
    registry.record(identity)
    registry.record(DeviceIdentity("CANDIDATE0000000001", "CANDIDATEUSER", CANDIDATE_PASSWORD))
    queue = MessageQueue(config.queue_db_path, config.max_queue_size)
    queue.enqueue("local-to-upstream", "dl/PLAF203/test/device/event/post", b'{"cmd":"HEART"}', 0)
    shadow = StateShadow(config.state_shadow_db_path)
    shadow.record_raw(DEVICE_ID, "dl/PLAF203/test/device/service/post", b'{"password":"hidden"}', "TEST")
    shadow.record_raw(DEVICE_ID, "dl/PLAF203/test/device/ntp/post", b'{"cmd":"NTP","ts":1}', "NTP")
    responder = LocalResponder(config.local_responder, shadow, config.handled_msg_id_ttl_seconds)
    logs = RingBufferLogHandler()
    logs.setFormatter(logging.Formatter("%(message)s"))
    telemetry = RelayTelemetry()
    telemetry.increment("ntp_requests")
    context = DashboardContext(config, registry, queue, shadow, telemetry, logs)
    context.set_active_device(identity, responder)
    yield context, logs
    queue.close()
    registry.close()
    shadow.close()


@pytest.fixture
def client(dashboard: tuple[DashboardContext, RingBufferLogHandler]) -> TestClient:
    """Build a FastAPI test client without opening a real HTTP listener."""
    context, _ = dashboard
    return TestClient(create_app(context))


@pytest.mark.parametrize(
    "path",
    [
        "/api/status",
        "/api/cloud",
        "/api/devices",
        "/api/queues",
        "/api/state",
        "/api/ntp",
        "/api/logs",
        "/api/system",
    ],
)
def test_read_only_api_endpoints_return_json(client: TestClient, path: str) -> None:
    """Every documented read-only endpoint returns a valid success payload."""
    response = client.get(path)

    assert response.status_code == 200
    assert isinstance(response.json(), dict)


def test_health_remains_ok_when_petlibro_is_down(client: TestClient) -> None:
    """Cloud failure must not make Docker/Kubernetes mark the local relay unhealthy."""
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["healthy"] is True
    assert response.json()["upstream_petlibro"] is False


def test_api_masks_credentials_and_sanitizes_raw_state(client: TestClient) -> None:
    """No learned password or full username can escape through read endpoints."""
    payload = " ".join(
        client.get(path).text for path in ("/api/devices", "/api/state", "/api/status", "/api/logs")
    )

    assert PASSWORD not in payload
    assert CANDIDATE_PASSWORD not in payload
    assert USERNAME not in payload
    assert "<redacted>" in client.get("/api/state").text


def test_candidate_and_disabled_local_responder_are_visible_without_secrets(client: TestClient) -> None:
    """Candidates are observable, while the default pure-pipe mode remains explicit."""
    devices = client.get("/api/devices").json()
    status = client.get("/api/status").json()

    assert devices["candidates"][0]["status"] == "CANDIDATE"
    assert devices["candidates"][0]["username"] != "CANDIDATEUSER"
    assert status["local_responder"]["enabled"] is False
    assert status["relay"]["mode"] == "PURE_PIPE"


def test_ntp_request_without_cloud_reply_is_shown_as_session_establishment(client: TestClient) -> None:
    """NTP is visible as a request; no reply is inferred when none was received."""
    payload = client.get("/api/ntp").json()

    assert payload["trigger"] == "session_establishment"
    assert payload["requests_observed"] == 1
    assert payload["cloud_ntp_sync_responses"] == 0
    assert payload["last_request"]["cmd"] == "NTP"
    assert payload["last_ntp_sync"] is None


def test_dashboard_has_no_write_routes(dashboard: tuple[DashboardContext, RingBufferLogHandler]) -> None:
    """The dashboard must not expose feeder control through HTTP."""
    context, _ = dashboard
    app = create_app(context)

    assert all(route.methods is None or route.methods <= {"GET", "HEAD"} for route in app.routes)


def test_sse_emits_new_sanitized_log(dashboard: tuple[DashboardContext, RingBufferLogHandler]) -> None:
    """The SSE generator publishes a newly appended safe log record."""
    context, logs = dashboard
    record = logging.makeLogRecord({"msg": f"password={PASSWORD}", "levelno": logging.INFO, "levelname": "INFO"})
    logs.emit(record)

    event = next(_stream_logs(context, after=0))

    assert event.startswith("id: 1\nevent: log\ndata: ")
    assert PASSWORD not in event
    assert "<redacted>" in event
