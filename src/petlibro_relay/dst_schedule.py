"""Daylight-saving schedule computation for the local NTP responder.

The cloud's `NTP_SYNC` on firmware V3.0.30 does not just carry the current UTC
offset - it also tells the device when the next two DST transitions happen and
what the offset becomes, so the device can keep its feeding schedule correct
across a transition without needing the cloud at that moment.

`zoneinfo` has no "next transition" API, so transitions are located by walking
forward: daily steps to find the day an offset change lands on, then hourly
within that day to pin the hour.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SEARCH_HORIZON_DAYS = 800  # comfortably covers two transitions in any zone
HOURS_PER_DAY = 24
MILLISECONDS_PER_SECOND = 1000


@dataclass(frozen=True, slots=True)
class DstSchedule:
    """Current UTC offset plus the next two transitions, as the cloud reports them."""

    offset_seconds: int
    next_offset_seconds: int | None
    next_transition_ts_ms: int | None
    second_next_offset_seconds: int | None
    second_next_transition_ts_ms: int | None


def _offset_seconds_at(zone: ZoneInfo, moment: datetime) -> int:
    offset = moment.astimezone(zone).utcoffset()
    return int(offset.total_seconds()) if offset is not None else 0


def _find_transitions(zone: ZoneInfo, start: datetime, wanted: int) -> list[tuple[datetime, int]]:
    """Return up to `wanted` upcoming (instant, new offset) transitions after `start`."""
    transitions: list[tuple[datetime, int]] = []
    current_offset = _offset_seconds_at(zone, start)
    # Transitions land on an exact UTC hour boundary, so walk from one. Starting
    # mid-hour would carry the current minutes/seconds into every result and
    # report a transition instant that is off by up to an hour.
    day = start.replace(minute=0, second=0, microsecond=0)
    for _ in range(SEARCH_HORIZON_DAYS):
        next_day = day + timedelta(days=1)
        next_day_offset = _offset_seconds_at(zone, next_day)
        if next_day_offset != current_offset:
            # The change happens within this day - narrow it down to the hour.
            hour = day
            for _ in range(HOURS_PER_DAY):
                next_hour = hour + timedelta(hours=1)
                if _offset_seconds_at(zone, next_hour) != current_offset:
                    transitions.append((next_hour, _offset_seconds_at(zone, next_hour)))
                    current_offset = _offset_seconds_at(zone, next_hour)
                    break
                hour = next_hour
            if len(transitions) == wanted:
                return transitions
        day = next_day
    return transitions


def compute(zone_name: str, now: datetime | None = None) -> DstSchedule:
    """Compute the DST schedule a device needs for the given IANA time zone.

    Args:
        zone_name: IANA zone name, e.g. "Europe/Paris".
        now: Instant to compute from; defaults to the current time.

    Returns:
        The current offset and the next two transitions. Transition fields are
        `None` for zones that do not observe DST.
    """
    zone = ZoneInfo(zone_name)
    moment = now or datetime.now(timezone.utc)
    transitions = _find_transitions(zone, moment, wanted=2)

    def as_ms(instant: datetime) -> int:
        return int(instant.timestamp() * MILLISECONDS_PER_SECOND)

    return DstSchedule(
        offset_seconds=_offset_seconds_at(zone, moment),
        next_offset_seconds=transitions[0][1] if len(transitions) > 0 else None,
        next_transition_ts_ms=as_ms(transitions[0][0]) if len(transitions) > 0 else None,
        second_next_offset_seconds=transitions[1][1] if len(transitions) > 1 else None,
        second_next_transition_ts_ms=as_ms(transitions[1][0]) if len(transitions) > 1 else None,
    )
