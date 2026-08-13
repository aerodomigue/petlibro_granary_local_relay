"""Deterministic tests for controlled device-to-cloud backlog replay."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from petlibro_relay.delivery_pump import DeliveryPump, PumpTarget
from petlibro_relay.device_context import LOCAL_TO_UPSTREAM
from petlibro_relay.message_queue import MessageQueue
from petlibro_relay.observability.telemetry import DeviceTelemetry


@dataclass
class ManualClock:
    """Controllable clock shared by a scheduler test."""

    now: float = 0.0

    def __call__(self) -> float:
        """Return the current simulated time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move simulated time forward."""
        self.now += seconds


@dataclass
class FakePublishResult:
    """Synchronous successful Paho publication result."""

    rc: int = 0

    def wait_for_publish(self, timeout: float | None = None) -> None:
        """Complete immediately without touching a network."""

    def is_published(self) -> bool:
        """Report successful publication."""
        return True


@dataclass
class FakeClient:
    """Fake cloud client that records only confirmed publishes."""

    connected: bool = True
    published: list[tuple[str, bytes]] = field(default_factory=list)

    def is_connected(self) -> bool:
        """Return the selected simulated connection state."""
        return self.connected

    def publish(self, topic: str, payload: bytes, qos: int = 0) -> FakePublishResult:
        """Record a publication without opening a socket."""
        self.published.append((topic, payload))
        return FakePublishResult()


def _topic(device_id: str) -> str:
    """Return one valid device-to-cloud topic."""
    return f"dl/PLAF203/{device_id}/device/event/post"


def _payload(sequence: int) -> bytes:
    """Build a recognisable durable test event."""
    return json.dumps({"cmd": "ERROR_EVENT", "sequence": sequence}).encode()


def _enqueue(queue: MessageQueue, device_id: str, count: int, is_live: bool = False) -> None:
    """Fill one device queue with durable events."""
    for sequence in range(count):
        queue.enqueue(
            device_id,
            LOCAL_TO_UPSTREAM,
            _topic(device_id),
            _payload(sequence),
            qos=0,
            is_live=is_live,
        )


def _pump(
    queue: MessageQueue,
    targets: list[PumpTarget],
    clock: ManualClock,
    rate_per_device: float = 5.0,
    rate_global: float = 20.0,
    start_delay: float = 0.0,
) -> DeliveryPump:
    """Build a deterministic controlled upstream replay pump."""
    return DeliveryPump(
        LOCAL_TO_UPSTREAM,
        queue,
        targets=lambda: targets,
        is_cloud_to_device=False,
        replay_rate_per_device=rate_per_device,
        replay_rate_global=rate_global,
        replay_start_delay_seconds=start_delay,
        replay_jitter=0.0,
        clock=clock,
        random_source=lambda: 0.5,
    )


def _target(device_id: str, client: FakeClient) -> PumpTarget:
    """Build one replay target backed by a fake client."""
    return PumpTarget(device_id, client, DeviceTelemetry(device_id))


def _drain_ticks(pump: DeliveryPump, clock: ManualClock, ticks: int, interval: float) -> None:
    """Advance and schedule a fixed number of deterministic ticks."""
    for _ in range(ticks):
        pump._run_once()
        clock.advance(interval)


def test_replay_waits_for_stabilization_and_does_not_burst(tmp_path: Path) -> None:
    """A 100-row backlog emits nothing before delay and one row per rate slot."""
    queue = MessageQueue(str(tmp_path / "queue.sqlite3"), max_size_per_direction=200)
    client = FakeClient()
    clock = ManualClock()
    try:
        _enqueue(queue, "A", 100)
        pump = _pump(queue, [_target("A", client)], clock, start_delay=1.5)

        pump._run_once()
        assert client.published == []
        clock.advance(1.5)
        pump._run_once()
        pump._run_once()
        assert len(client.published) == 1
        clock.advance(0.2)
        pump._run_once()
        assert len(client.published) == 2
        assert queue.count("A", LOCAL_TO_UPSTREAM) == 98
    finally:
        queue.close()


def test_global_rate_and_rotation_fairly_share_backlog(tmp_path: Path) -> None:
    """Global budget yields A, B, C rather than draining A first."""
    queue = MessageQueue(str(tmp_path / "queue.sqlite3"), max_size_per_direction=100)
    clients = [FakeClient(), FakeClient(), FakeClient()]
    targets = [_target(device_id, client) for device_id, client in zip("ABC", clients)]
    clock = ManualClock()
    try:
        for device_id in "ABC":
            _enqueue(queue, device_id, 20)
        pump = _pump(queue, targets, clock, rate_per_device=20.0, rate_global=10.0)

        _drain_ticks(pump, clock, ticks=3, interval=0.1)

        assert [len(client.published) for client in clients] == [1, 1, 1]
        assert sum(len(client.published) for client in clients) == 3
    finally:
        queue.close()


def test_jitter_never_allows_a_rate_above_its_configured_cap(tmp_path: Path) -> None:
    """Jitter can spread replay traffic out but cannot make it burst faster."""
    queue = MessageQueue(str(tmp_path / "queue.sqlite3"), max_size_per_direction=20)
    clock = ManualClock()
    try:
        pump = DeliveryPump(
            LOCAL_TO_UPSTREAM,
            queue,
            targets=lambda: [],
            is_cloud_to_device=False,
            replay_rate_per_device=5.0,
            replay_rate_global=20.0,
            replay_jitter=0.15,
            clock=clock,
            random_source=lambda: 0.0,
        )

        assert pump._jittered_interval(5.0) >= 0.2
        assert pump._jittered_interval(20.0) >= 0.05
    finally:
        queue.close()


def test_small_backlog_completes_without_waiting_for_large_device(tmp_path: Path) -> None:
    """Three rows for B are interleaved with, not starved by, A's 100 rows."""
    queue = MessageQueue(str(tmp_path / "queue.sqlite3"), max_size_per_direction=200)
    client_a = FakeClient()
    client_b = FakeClient()
    clock = ManualClock()
    try:
        _enqueue(queue, "A", 100)
        _enqueue(queue, "B", 3)
        pump = _pump(
            queue,
            [_target("A", client_a), _target("B", client_b)],
            clock,
            rate_per_device=100.0,
            rate_global=100.0,
        )

        _drain_ticks(pump, clock, ticks=6, interval=0.01)

        assert len(client_b.published) == 3
        assert len(client_a.published) == 3
        assert queue.count("A", LOCAL_TO_UPSTREAM) == 97
    finally:
        queue.close()


def test_live_row_is_sent_before_backlog_and_before_replay_delay(tmp_path: Path) -> None:
    """A live event is never held behind an old backlog of the same device."""
    queue = MessageQueue(str(tmp_path / "queue.sqlite3"), max_size_per_direction=20)
    client = FakeClient()
    clock = ManualClock()
    try:
        _enqueue(queue, "A", 3)
        queue.enqueue("A", LOCAL_TO_UPSTREAM, _topic("A"), _payload(999), qos=0, is_live=True)
        pump = _pump(queue, [_target("A", client)], clock, start_delay=10.0)

        pump._run_once()

        assert json.loads(client.published[0][1])["sequence"] == 999
        assert queue.backlog_count("A", LOCAL_TO_UPSTREAM) == 3
    finally:
        queue.close()


def test_backlog_waits_for_upstream_subscription_confirmation(tmp_path: Path) -> None:
    """The settle timer starts only after the target reports replay readiness."""
    queue = MessageQueue(str(tmp_path / "queue.sqlite3"), max_size_per_direction=20)
    client = FakeClient()
    clock = ManualClock()
    targets = [_target("A", client)]
    targets[0] = PumpTarget("A", client, targets[0].telemetry, replay_ready=False)
    try:
        _enqueue(queue, "A", 1)
        pump = _pump(queue, targets, clock, start_delay=1.0)

        pump._run_once()
        assert client.published == []
        targets[0] = PumpTarget("A", client, targets[0].telemetry, replay_ready=True)
        pump._run_once()
        clock.advance(1.0)
        pump._run_once()

        assert len(client.published) == 1
    finally:
        queue.close()


def test_disconnect_pauses_only_its_replay_and_keeps_remaining_rows(tmp_path: Path) -> None:
    """A disconnected device retains its backlog while another keeps replaying."""
    queue = MessageQueue(str(tmp_path / "queue.sqlite3"), max_size_per_direction=20)
    client_a = FakeClient()
    client_b = FakeClient()
    clock = ManualClock()
    try:
        _enqueue(queue, "A", 3)
        _enqueue(queue, "B", 3)
        pump = _pump(
            queue,
            [_target("A", client_a), _target("B", client_b)],
            clock,
            rate_per_device=100.0,
            rate_global=100.0,
        )
        pump._run_once()
        client_a.connected = False
        clock.advance(0.01)
        pump._run_once()
        clock.advance(0.01)
        pump._run_once()

        assert queue.count("A", LOCAL_TO_UPSTREAM) == 2
        assert len(client_b.published) == 2
    finally:
        queue.close()


def test_unsent_live_row_becomes_rate_limited_backlog_after_disconnect(tmp_path: Path) -> None:
    """A live row that misses its session cannot bypass the next replay window."""
    queue = MessageQueue(str(tmp_path / "queue.sqlite3"), max_size_per_direction=20)
    client = FakeClient(connected=False)
    clock = ManualClock()
    try:
        _enqueue(queue, "A", 1, is_live=True)
        pump = _pump(queue, [_target("A", client)], clock, start_delay=1.0)

        pump._run_once()
        client.connected = True
        pump._run_once()
        assert client.published == []
        clock.advance(1.0)
        pump._run_once()

        assert len(client.published) == 1
    finally:
        queue.close()


def test_expired_backlog_is_dropped_before_publish(tmp_path: Path) -> None:
    """Expiry is checked at replay selection time, not only at queue insertion."""
    queue = MessageQueue(str(tmp_path / "queue.sqlite3"), max_size_per_direction=20)
    client = FakeClient()
    now = time.time() + 10.0
    clock = ManualClock(now)
    telemetry = DeviceTelemetry("A")
    try:
        queue.enqueue(
            "A",
            LOCAL_TO_UPSTREAM,
            _topic("A"),
            _payload(1),
            qos=0,
            max_age_seconds=1.0,
        )
        pump = _pump(queue, [PumpTarget("A", client, telemetry)], clock)

        pump._run_once()

        assert client.published == []
        assert queue.count("A", LOCAL_TO_UPSTREAM) == 0
        assert telemetry.snapshot()["upstream"]["counters"]["replay_dropped_expired"] == 1
    finally:
        queue.close()


def test_latest_wins_rows_stay_coalesced_for_replay(tmp_path: Path) -> None:
    """The scheduler reuses the queue's existing coalescing rather than replaying stale state."""
    queue = MessageQueue(str(tmp_path / "queue.sqlite3"), max_size_per_direction=20)
    client = FakeClient()
    clock = ManualClock()
    try:
        for value in (False, True, False):
            queue.enqueue(
                "A",
                LOCAL_TO_UPSTREAM,
                _topic("A"),
                json.dumps({"cmd": "ATTR_SET_SERVICE", "soundSwitch": value}).encode(),
                qos=0,
                coalesce_key=f"{_topic('A')}|ATTR_SET_SERVICE",
            )
        pump = _pump(queue, [_target("A", client)], clock)

        pump._run_once()

        assert len(client.published) == 1
        assert json.loads(client.published[0][1])["soundSwitch"] is False
    finally:
        queue.close()
