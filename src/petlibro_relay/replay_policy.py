"""Decides whether a queued message is still safe to deliver after an outage.

Durably queueing everything and replaying it oldest-first is correct for
telemetry flowing device -> cloud: an event that happened at 14:00 is still
true at 17:00, it just arrives late.

It is *not* correct for commands flowing cloud -> device. Those act on the
physical world at the moment they are delivered, not at the moment they were
issued. Replaying a `MANUAL_FEEDING_SERVICE` that a user pressed at 14:00,
after a three-hour cloud outage, feeds the cat at 17:00 - an action nobody
asked for. Likewise a stale `NTP` response would set the clock to a time
that has since passed, and a replayed `DEVICE_REBOOT` or `OTA_UPGRADE` would
fire at an arbitrary later moment.

So each cloud -> device command gets an explicit policy:

* `never_replay`  - only meaningful if delivered essentially immediately.
  Given a small grace window for normal in-flight latency, then dropped.
* a TTL           - meaningful for a bounded time (a manual feed the user
  pressed a minute ago is plausibly still wanted; an hour ago is not).
* `coalesce`      - state-carrying, latest-wins. Queueing five successive
  settings updates and replaying all five is pointless and racy; only the
  most recent one describes the intended state, so an incoming message
  supersedes any older pending one sharing its coalesce key.
* the default     - durable FIFO, no expiry.

Anything unrecognised falls through to the default: unknown commands are
forwarded rather than silently dropped, since dropping a command we don't
understand is worse than delivering it late.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

# Grace window for a "never replay" command: long enough to cover normal
# publish latency on a healthy connection, short enough that anything held
# through an actual outage is dropped.
NEVER_REPLAY_GRACE_SECONDS = 5.0

# A user-initiated manual feed stays plausible for about a minute.
MANUAL_FEED_TTL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ReplayPolicy:
    """How a message should be treated if it could not be delivered immediately."""

    max_age_seconds: float | None = None
    coalesce: bool = False


DURABLE_FIFO = ReplayPolicy()
NEVER_REPLAY = ReplayPolicy(max_age_seconds=NEVER_REPLAY_GRACE_SECONDS)
LATEST_WINS = ReplayPolicy(coalesce=True)
MANUAL_FEED = ReplayPolicy(max_age_seconds=MANUAL_FEED_TTL_SECONDS)

# Command names per the protocol documented in icex2/plaf203 and confirmed
# against this device's own traffic.
_CLOUD_TO_DEVICE_POLICIES: dict[str, ReplayPolicy] = {
    # Time-critical: a stale clock sync is worse than no clock sync.
    "NTP": NEVER_REPLAY,
    "NTP_SYNC": NEVER_REPLAY,
    # Disruptive one-shot actions - never fire these at an arbitrary later time.
    "DEVICE_REBOOT": NEVER_REPLAY,
    "RESET": NEVER_REPLAY,
    "RESTORE": NEVER_REPLAY,
    "OTA_UPGRADE": NEVER_REPLAY,
    "OTA_INFORM": NEVER_REPLAY,
    "OTA_PROGRESS": NEVER_REPLAY,
    "UNBIND": NEVER_REPLAY,
    "BINDING": NEVER_REPLAY,
    "WIFI_CHANGE_SERVICE": NEVER_REPLAY,
    "WIFI_RECONNECT_SERVICE": NEVER_REPLAY,
    "INITIALIZE_SD_CARD_SERVICE": NEVER_REPLAY,
    # Physical action, bounded relevance.
    "MANUAL_FEEDING_SERVICE": MANUAL_FEED,
    # State-carrying: only the most recent value matters.
    "ATTR_SET_SERVICE": LATEST_WINS,
    "ATTR_GET_SERVICE": LATEST_WINS,
    "FEEDING_PLAN_SERVICE": LATEST_WINS,
    "DEVICE_FEEDING_PLAN_SERVICE": LATEST_WINS,
    "DEVICE_CONFIG_SYNC": LATEST_WINS,
    "SERVER_CONFIG_PUSH": LATEST_WINS,
    "GET_CONFIG": LATEST_WINS,
    "DEVICE_PROPERTIES_SERVICE": LATEST_WINS,
    "DEVICE_INFO_SERVICE": LATEST_WINS,
    "TUTK_CONTRACT_SERVICE": LATEST_WINS,
}


def extract_command(payload: bytes) -> str | None:
    """Return the `cmd` field of a JSON MQTT payload, or None if absent/unparseable.

    Args:
        payload: Raw MQTT message payload.

    Returns:
        The command name, or `None` if the payload isn't JSON or has no
        string `cmd` field.
    """
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    command = decoded.get("cmd")
    return command if isinstance(command, str) else None


def policy_for(is_cloud_to_device: bool, command: str | None) -> ReplayPolicy:
    """Return the replay policy for a message.

    Args:
        is_cloud_to_device: True for the cloud -> device direction, where
            commands act on the physical world when delivered. Device ->
            cloud traffic is telemetry and always uses durable FIFO.
        command: The message's `cmd` field, if any.

    Returns:
        The applicable `ReplayPolicy`.
    """
    if not is_cloud_to_device or command is None:
        return DURABLE_FIFO
    return _CLOUD_TO_DEVICE_POLICIES.get(command, DURABLE_FIFO)


def coalesce_key_for(topic: str, command: str | None, policy: ReplayPolicy) -> str | None:
    """Return the key superseding older pending messages, or None if not coalescing.

    Args:
        topic: The message's MQTT topic.
        command: The message's `cmd` field, if any.
        policy: The policy resolved for this message.

    Returns:
        A key identifying messages this one supersedes, or `None`.
    """
    if not policy.coalesce or command is None:
        return None
    return f"{topic}|{command}"
