"""Answers a subset of the feeder's requests locally when the cloud is unreachable.

The relay stays a transparent pipe whenever PETLIBRO is actually reachable.
This component only steps in while it is not, and only for requests whose
protocol we have confirmed against real traffic. Everything else is forwarded
and, if it cannot be, simply goes unanswered - inventing a reply to a command
we do not fully understand risks putting the device into a state nobody can
debug, which is worse than the device retrying later.

What may be answered locally, and on whose authority:

| Command                | Source of the answer                                  |
|------------------------|-------------------------------------------------------|
| `NTP`                  | generated locally - clocks are a purely local service |
| `FEEDING_PLAN_SERVICE` | last complete plan set the cloud sent (last-known-good)|
| `ATTR_GET_SERVICE`     | last settings the cloud pushed (last-known-good)       |

Never answered locally, by design: `MANUAL_FEEDING_SERVICE`, `DEVICE_REBOOT`,
`RESET`, `RESTORE`, OTA, Wi-Fi and binding commands. Those act on the physical
world or on the device's cloud association; only the real cloud may order
them, and a stale or synthesised one could feed an animal or unbind a device
at an arbitrary moment.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum, auto
from typing import Any

from . import protocol
from .dst_schedule import compute as compute_dst_schedule
from .state_shadow import StateShadow

_LOGGER = logging.getLogger(__name__)

MILLISECONDS_PER_SECOND = 1000
SECONDS_PER_HOUR = 3600


class UpstreamState(Enum):
    """How far along the upstream MQTT connection actually is.

    A completed TCP handshake means nothing here: PETLIBRO has been observed
    accepting the connection, never answering the CONNECT, and resetting ~30s
    later. Only `ONLINE` - CONNACK 0 received and the session live - counts as
    the cloud being available.
    """

    DISCONNECTED = auto()
    TCP_CONNECTING = auto()
    MQTT_CONNECTING = auto()
    ONLINE = auto()

    @property
    def is_online(self) -> bool:
        """True only once the MQTT session is genuinely established."""
        return self is UpstreamState.ONLINE


class Decision(Enum):
    """What the relay should do with a message from the feeder."""

    FORWARD_ONLY = auto()
    CACHE_AND_FORWARD = auto()
    RESPOND_LOCAL = auto()
    IGNORE = auto()


@dataclass(frozen=True, slots=True)
class ResponderAction:
    """The decision plus, when responding locally, the reply to publish."""

    decision: Decision
    response_topic: str | None = None
    response_payload: bytes | None = None
    handled_msg_id: str | None = None


@dataclass(slots=True)
class ResponderCounters:
    """Simple counters for observability."""

    local_responses: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    unknown_requests: int = 0
    suppressed_late_cloud_responses: int = 0


@dataclass(frozen=True, slots=True)
class LocalResponderSettings:
    """Per-function feature flags. Conservative defaults: everything off."""

    enabled: bool = False
    ntp: bool = False
    config: bool = False
    feeding_plan: bool = False
    always_answer_ntp_locally: bool = False
    device_timezone: str = "UTC"
    # Below this drift the device's clock is considered correct and no
    # NTP_SYNC is sent, matching the cloud's observed behaviour. 10s is the
    # threshold icex2 measured on a neighbouring firmware.
    clock_drift_tolerance_seconds: float = 10.0


class LocalResponder:
    """Decides whether a feeder request can be answered from local knowledge."""

    def __init__(
        self, settings: LocalResponderSettings, shadow: StateShadow, handled_msg_id_ttl_seconds: float
    ) -> None:
        """Initialize the responder.

        Args:
            settings: Feature flags and the device's IANA time zone.
            shadow: Persistent state shadow to read last-known-good from.
            handled_msg_id_ttl_seconds: How long a locally answered msgId is
                remembered so a late cloud answer to it can be suppressed.
        """
        self._settings = settings
        self._shadow = shadow
        self._handled_msg_id_ttl_seconds = handled_msg_id_ttl_seconds
        self._handled_msg_ids: dict[str, float] = {}
        self.counters = ResponderCounters()

    def snapshot(self) -> dict[str, Any]:
        """Return feature flags and counters for read-only observability."""
        return {
            "enabled": self._settings.enabled,
            "ntp_enabled": self._settings.ntp,
            "config_enabled": self._settings.config,
            "feeding_plan_enabled": self._settings.feeding_plan,
            "always_answer_ntp_locally": self._settings.always_answer_ntp_locally,
            "device_timezone": self._settings.device_timezone,
            "counters": asdict(self.counters),
        }

    # -- cloud -> device: learn ---------------------------------------------------

    def observe_cloud_message(self, device_id: str, topic: str, payload: bytes) -> None:
        """Learn last-known-good state from a message the cloud sent to the device."""
        command, body = _parse(payload)
        self._shadow.record_raw(device_id, topic, payload, command)
        if body is None:
            return

        if command == protocol.Command.FEEDING_PLAN_SERVICE:
            plans = body.get("plans")
            # Only a full plan definition is worth keeping: the device's own
            # "/post" carries plan ids with no schedule, and storing that would
            # overwrite the real plans with useless stubs.
            if isinstance(plans, list) and all(_is_complete_plan(p) for p in plans) and plans:
                self._shadow.update_feeding_plans(device_id, plans, body.get("msgId"))
        elif command in (
            protocol.Command.ATTR_SET_SERVICE,
            protocol.Command.SERVER_CONFIG_PUSH,
            protocol.Command.DEVICE_CONFIG_SYNC,
        ):
            settings = {k: v for k, v in body.items() if k not in ("cmd", "ts", "msgId")}
            if settings:
                self._shadow.update_desired(device_id, settings)

    def is_suppressed_cloud_response(self, payload: bytes) -> bool:
        """True if this cloud message answers a request already served locally.

        Prevents the device receiving two answers to one request when the cloud
        comes back mid-flight.
        """
        _, body = _parse(payload)
        if body is None:
            return False
        msg_id = body.get("msgId")
        if not isinstance(msg_id, str):
            return False
        self._expire_handled_msg_ids()
        if msg_id in self._handled_msg_ids:
            self.counters.suppressed_late_cloud_responses += 1
            _LOGGER.info("SUPPRESSED late cloud response msgId=%s (already answered locally)", msg_id)
            return True
        return False

    # -- device -> cloud: decide --------------------------------------------------

    def decide(
        self, device_id: str, topic: str, payload: bytes, upstream: UpstreamState
    ) -> ResponderAction:
        """Decide what to do with a message the feeder published.

        Args:
            device_id: The active device's id.
            topic: Topic the feeder published on.
            payload: Raw payload.
            upstream: Real upstream connection state.

        Returns:
            The action to take, including a reply to publish when answering
            locally.
        """
        command, body = _parse(payload)
        self._shadow.record_raw(device_id, topic, payload, command)
        if body is not None and command is not None:
            self._record_reported(device_id, command, body)

        if not self._settings.enabled or body is None or command is None:
            return ResponderAction(Decision.CACHE_AND_FORWARD)

        # The device acknowledges an NTP_SYNC by posting back the same msgId.
        # When the sync came from us rather than the cloud, that ack must not
        # be forwarded: the cloud never issued that msgId and would be acked
        # for a message it did not send.
        if self._is_ack_for_local_message(command, body):
            _LOGGER.debug("Swallowing device ack for locally generated %s", command)
            return ResponderAction(Decision.IGNORE)

        may_answer_ntp = command == protocol.Command.NTP and self._settings.ntp and (
            self._settings.always_answer_ntp_locally or not upstream.is_online
        )
        if may_answer_ntp:
            if not self._device_clock_has_drifted(body):
                # Mirror the cloud: an NTP post whose clock is already correct
                # gets no answer. Observed on real traffic - the cloud pushes
                # NTP_SYNC on session start and when it sees drift, but left a
                # healthy device's NTP request unanswered.
                _LOGGER.debug("Device clock within tolerance, no NTP_SYNC needed")
                return ResponderAction(Decision.CACHE_AND_FORWARD)
            return self._respond_ntp(device_id)

        if upstream.is_online:
            # Cloud is reachable: stay transparent, just keep learning.
            return ResponderAction(Decision.CACHE_AND_FORWARD)

        if command == protocol.Command.FEEDING_PLAN_SERVICE and self._settings.feeding_plan:
            return self._respond_feeding_plan(device_id, body)
        if command == protocol.Command.ATTR_GET_SERVICE and self._settings.config:
            return self._respond_config(device_id, body)

        if command in _NEVER_LOCAL:
            _LOGGER.info("NO LOCAL RESPONSE for %s (never answered locally by design)", command)
        else:
            self.counters.unknown_requests += 1
            _LOGGER.info("NO LOCAL RESPONSE unknown cmd=%s", command)
        return ResponderAction(Decision.CACHE_AND_FORWARD)

    def _device_clock_has_drifted(self, body: dict[str, Any]) -> bool:
        """True if the device's reported time is far enough off to warrant a resync.

        The device puts its current clock in the `ts` of its NTP post. A
        request with no usable timestamp is treated as drifted, so an
        unparseable clock still gets corrected rather than silently ignored.
        """
        device_ts = body.get("ts")
        if not isinstance(device_ts, (int, float)):
            return True
        drift_seconds = abs(time.time() - device_ts / MILLISECONDS_PER_SECOND)
        return drift_seconds > self._settings.clock_drift_tolerance_seconds

    def _is_ack_for_local_message(self, command: str, body: dict[str, Any]) -> bool:
        """True if this device message acknowledges something we generated ourselves."""
        if command != protocol.Command.NTP_SYNC:
            return False
        msg_id = body.get("msgId")
        if not isinstance(msg_id, str):
            return False
        self._expire_handled_msg_ids()
        return msg_id in self._handled_msg_ids

    def _record_reported(self, device_id: str, command: str, body: dict[str, Any]) -> None:
        """Store physical facts the device reported. Never synthesised."""
        if command == protocol.Command.HEARTBEAT:
            self._shadow.update_reported(
                device_id,
                {
                    "last_heartbeat_ts": body.get("ts"),
                    "rssi": body.get("rssi"),
                    "wifi_type": body.get("wifiType"),
                },
            )
        elif command == protocol.Command.DEVICE_START_EVENT:
            self._shadow.update_reported(
                device_id,
                {
                    "firmware": body.get("softwareVersion"),
                    "hardware_version": body.get("hardwareVersion"),
                    "mac": body.get("mac"),
                },
            )

    # -- response builders --------------------------------------------------------

    def _respond_ntp(self, device_id: str) -> ResponderAction:
        """Build an NTP_SYNC reply from the local clock and DST rules.

        Shape follows this firmware's own cloud traffic (V3.0.30), which
        carries DST transition fields that icex2's older firmware does not.
        """
        schedule = compute_dst_schedule(self._settings.device_timezone)
        msg_id = uuid.uuid4().hex
        payload: dict[str, Any] = {
            "cmd": protocol.Command.NTP_SYNC,
            "ts": int(time.time() * MILLISECONDS_PER_SECOND),
            "msgId": msg_id,
            "timezoneOffsetSeconds": schedule.offset_seconds,
            "timezone": schedule.offset_seconds // SECONDS_PER_HOUR,
        }
        if schedule.next_transition_ts_ms is not None:
            payload["nextDSTOffsetSeconds"] = schedule.next_offset_seconds
            payload["nextDSTTransitionTs"] = schedule.next_transition_ts_ms
        if schedule.second_next_transition_ts_ms is not None:
            payload["secondNextDSTOffsetSeconds"] = schedule.second_next_offset_seconds
            payload["secondNextDSTTransitionTs"] = schedule.second_next_transition_ts_ms

        self.counters.local_responses += 1
        _LOGGER.info("LOCAL RESPONSE NTP msgId=%s offset=%ds", msg_id, schedule.offset_seconds)
        # Remember the msgId we minted: the device acks an NTP_SYNC by posting
        # the same msgId back, and that ack must be swallowed rather than sent
        # to a cloud that never issued it. (Observed on real traffic: cloud
        # pushes NTP_SYNC, device replies NTP_SYNC/code=0 with the same msgId.)
        return self._reply(device_id, "ntp", payload, handled_msg_id=msg_id)

    def _respond_feeding_plan(self, device_id: str, body: dict[str, Any]) -> ResponderAction:
        """Reply with the last complete plan set the cloud sent, if we have one."""
        stored = self._shadow.get_feeding_plans(device_id)
        msg_id = body.get("msgId")
        if stored is None or not stored.plans:
            self.counters.cache_misses += 1
            _LOGGER.warning("NO LOCAL RESPONSE for FEEDING_PLAN_SERVICE: no cached plan set")
            return ResponderAction(Decision.CACHE_AND_FORWARD)
        if not isinstance(msg_id, str):
            _LOGGER.warning("NO LOCAL RESPONSE for FEEDING_PLAN_SERVICE: request carried no msgId")
            return ResponderAction(Decision.CACHE_AND_FORWARD)

        self.counters.cache_hits += 1
        self.counters.local_responses += 1
        _LOGGER.info("LOCAL RESPONSE feeding_plan msgId=%s (%d plan(s))", msg_id, len(stored.plans))
        # Same msgId as the request: this is the answer to that specific
        # question, not a new instruction to feed.
        payload = {
            "cmd": protocol.Command.FEEDING_PLAN_SERVICE,
            "ts": int(time.time() * MILLISECONDS_PER_SECOND),
            "msgId": msg_id,
            "plans": stored.plans,
        }
        return self._reply(device_id, "service", payload, handled_msg_id=msg_id)

    def _respond_config(self, device_id: str, body: dict[str, Any]) -> ResponderAction:
        """Reply with the last settings the cloud pushed, if any are cached."""
        desired = self._shadow.get_desired(device_id)
        msg_id = body.get("msgId")
        if not desired:
            self.counters.cache_misses += 1
            _LOGGER.warning("NO LOCAL RESPONSE for ATTR_GET_SERVICE: no cached configuration")
            return ResponderAction(Decision.CACHE_AND_FORWARD)
        if not isinstance(msg_id, str):
            _LOGGER.warning("NO LOCAL RESPONSE for ATTR_GET_SERVICE: request carried no msgId")
            return ResponderAction(Decision.CACHE_AND_FORWARD)

        self.counters.cache_hits += 1
        self.counters.local_responses += 1
        _LOGGER.info("LOCAL RESPONSE GET_CONFIG msgId=%s (%d key(s))", msg_id, len(desired))
        payload: dict[str, Any] = {
            "cmd": protocol.Command.ATTR_SET_SERVICE,
            "ts": int(time.time() * MILLISECONDS_PER_SECOND),
            "msgId": msg_id,
            **desired,
        }
        return self._reply(device_id, "service", payload, handled_msg_id=msg_id)

    def _reply(
        self, device_id: str, category: str, payload: dict[str, Any], handled_msg_id: str | None
    ) -> ResponderAction:
        if handled_msg_id is not None:
            self._handled_msg_ids[handled_msg_id] = time.time()
        return ResponderAction(
            decision=Decision.RESPOND_LOCAL,
            response_topic=protocol.sub_topic(device_id, category),
            response_payload=json.dumps(payload).encode("utf-8"),
            handled_msg_id=handled_msg_id,
        )

    def _expire_handled_msg_ids(self) -> None:
        cutoff = time.time() - self._handled_msg_id_ttl_seconds
        for msg_id in [m for m, seen in self._handled_msg_ids.items() if seen < cutoff]:
            del self._handled_msg_ids[msg_id]


_NEVER_LOCAL = frozenset(
    {
        protocol.Command.MANUAL_FEEDING_SERVICE,
        protocol.Command.DEVICE_REBOOT,
        protocol.Command.RESET,
        protocol.Command.RESTORE,
    }
)

_REQUIRED_PLAN_FIELDS = ("planId", "executionTime", "grainNum")


def _is_complete_plan(plan: Any) -> bool:
    """True if a plan entry actually defines a schedule rather than just naming one."""
    return isinstance(plan, dict) and all(field in plan for field in _REQUIRED_PLAN_FIELDS)


def _parse(payload: bytes) -> tuple[str | None, dict[str, Any] | None]:
    """Return the payload's `cmd` and decoded body, or (None, None) if not JSON."""
    try:
        body = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None
    if not isinstance(body, dict):
        return None, None
    command = body.get("cmd")
    return (command if isinstance(command, str) else None), body
