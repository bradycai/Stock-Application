"""Pure scheduling tests: which period is due at `now` (UTC in, ET wall clock), across DST."""
from datetime import datetime, time, timedelta, timezone

import pytest

from app.services.scheduler.schedule import Schedule, due_period

U = timezone.utc
BRIEF = Schedule("weekday_at", at=time(8, 30), grace=timedelta(hours=6))
SCAN = Schedule("market_interval", interval_min=30)
WEEKLY = Schedule("weekly_at", at=time(10, 0), weekday=5, grace=timedelta(hours=36))


@pytest.mark.parametrize("sched,now,expected", [
    # briefing, winter (EST = UTC-5): Tue 2026-01-06
    (BRIEF, datetime(2026, 1, 6, 13, 29, tzinfo=U), None),
    (BRIEF, datetime(2026, 1, 6, 13, 30, tzinfo=U), "2026-01-06"),
    (BRIEF, datetime(2026, 1, 6, 19, 29, tzinfo=U), "2026-01-06"),
    (BRIEF, datetime(2026, 1, 6, 19, 30, tzinfo=U), None),       # past the grace window
    (BRIEF, datetime(2026, 1, 10, 14, 0, tzinfo=U), None),       # Saturday
    # DST starts Sun 2026-03-08: Fri 03-06 is EST, Mon 03-09 is EDT (08:30 = 12:30 UTC)
    (BRIEF, datetime(2026, 3, 6, 13, 30, tzinfo=U), "2026-03-06"),
    (BRIEF, datetime(2026, 3, 9, 12, 29, tzinfo=U), None),
    (BRIEF, datetime(2026, 3, 9, 12, 30, tzinfo=U), "2026-03-09"),
    # DST ends Sun 2026-11-01: Mon 11-02 is EST again (08:30 = 13:30 UTC)
    (BRIEF, datetime(2026, 10, 30, 12, 30, tzinfo=U), "2026-10-30"),
    (BRIEF, datetime(2026, 11, 2, 12, 30, tzinfo=U), None),
    (BRIEF, datetime(2026, 11, 2, 13, 30, tzinfo=U), "2026-11-02"),
    # scanner: 30-minute slots from 09:30 ET, weekdays only
    (SCAN, datetime(2026, 1, 6, 14, 29, tzinfo=U), None),        # 09:29 ET
    (SCAN, datetime(2026, 1, 6, 14, 30, tzinfo=U), "2026-01-06T09:30"),
    (SCAN, datetime(2026, 1, 6, 15, 17, tzinfo=U), "2026-01-06T10:00"),
    (SCAN, datetime(2026, 1, 6, 20, 59, tzinfo=U), "2026-01-06T15:30"),
    (SCAN, datetime(2026, 1, 6, 21, 0, tzinfo=U), None),         # 16:00 ET close
    (SCAN, datetime(2026, 1, 10, 15, 0, tzinfo=U), None),        # Saturday
    (SCAN, datetime(2026, 3, 9, 13, 29, tzinfo=U), None),        # 09:29 EDT
    (SCAN, datetime(2026, 3, 9, 13, 30, tzinfo=U), "2026-03-09T09:30"),
    # weekly: Saturday 10:00 ET
    (WEEKLY, datetime(2026, 1, 10, 14, 59, tzinfo=U), None),
    (WEEKLY, datetime(2026, 1, 10, 15, 0, tzinfo=U), "2026-01-10"),
    (WEEKLY, datetime(2026, 1, 11, 20, 0, tzinfo=U), "2026-01-10"),  # Sunday, inside grace
    (WEEKLY, datetime(2026, 1, 12, 15, 0, tzinfo=U), None),
    (WEEKLY, datetime(2026, 3, 7, 14, 59, tzinfo=U), None),      # Sat before DST: 10:00 EST = 15:00 UTC
    (WEEKLY, datetime(2026, 3, 7, 15, 0, tzinfo=U), "2026-03-07"),
    (WEEKLY, datetime(2026, 11, 7, 15, 0, tzinfo=U), "2026-11-07"),  # Sat after DST ends (EST)
    (WEEKLY, datetime(2026, 10, 31, 13, 59, tzinfo=U), None),    # Sat 09:59 EDT
    (WEEKLY, datetime(2026, 10, 31, 14, 0, tzinfo=U), "2026-10-31"),  # 10:00 EDT
])
def test_due_period(sched, now, expected):
    assert due_period(sched, now) == expected


def test_scanner_interval_changes_slots():
    s = Schedule("market_interval", interval_min=60)
    assert due_period(s, datetime(2026, 1, 6, 15, 59, tzinfo=U)) == "2026-01-06T10:30"
    assert due_period(s, datetime(2026, 1, 6, 16, 30, tzinfo=U)) == "2026-01-06T11:30"


def test_naive_now_rejected_and_unknown_kind():
    with pytest.raises(ValueError):
        due_period(BRIEF, datetime(2026, 1, 6, 13, 30))
    with pytest.raises(ValueError):
        due_period(Schedule("hourly"), datetime(2026, 1, 6, 13, 30, tzinfo=U))
