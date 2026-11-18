"""Pure scheduling: which period of a job is due at a given instant, in US/Eastern wall-clock
time (so 08:30 stays 08:30 across DST changes). No DB, no clock reads: callers pass `now`.

Three schedule kinds, each yielding a deterministic `period_key` (the idempotency key):
- weekday_at:       every weekday at HH:MM ET            -> key "YYYY-MM-DD"
- market_interval:  every N minutes from 09:30 to 16:00  -> key "YYYY-MM-DDTHH:MM" (slot start)
- weekly_at:        one weekday (Saturday=5) at HH:MM ET -> key "YYYY-MM-DD" (that day)

Only the most recent period is ever due, and only within `grace` of its scheduled time: a worker
that was down all morning does not run a stale briefing in the afternoon.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


@dataclass(frozen=True)
class Schedule:
    kind: str                      # weekday_at | market_interval | weekly_at
    at: time = time(0, 0)
    interval_min: int = 30
    weekday: int = 5               # weekly_at only (Mon=0 .. Sun=6)
    grace: timedelta = timedelta(hours=6)


def _local(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(ET).replace(tzinfo=None)  # naive ET wall clock


def due_period(s: Schedule, now: datetime) -> str | None:
    """The period key due at `now`, or None when nothing is due."""
    loc = _local(now)
    if s.kind == "weekday_at":
        if loc.weekday() >= 5:
            return None
        scheduled = datetime.combine(loc.date(), s.at)
        return loc.date().isoformat() if scheduled <= loc < scheduled + s.grace else None
    if s.kind == "weekly_at":
        day = loc.date() - timedelta(days=(loc.weekday() - s.weekday) % 7)
        scheduled = datetime.combine(day, s.at)
        return day.isoformat() if scheduled <= loc < scheduled + s.grace else None
    if s.kind == "market_interval":
        n = max(1, s.interval_min)
        if loc.weekday() >= 5 or not (MARKET_OPEN <= loc.time() < MARKET_CLOSE):
            return None
        mins = (loc.hour * 60 + loc.minute) - (MARKET_OPEN.hour * 60 + MARKET_OPEN.minute)
        slot = datetime.combine(loc.date(), MARKET_OPEN) + timedelta(minutes=(mins // n) * n)
        return slot.strftime("%Y-%m-%dT%H:%M")
    raise ValueError(f"unknown schedule kind {s.kind!r}")
