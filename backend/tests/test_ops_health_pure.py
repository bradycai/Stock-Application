"""Pure, table-driven tests for the health checks (no DB, no network)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.services.ops import health

NOW = datetime(2026, 1, 6, 15, 0, tzinfo=timezone.utc)  # Tuesday 10:00 ET


def ago(sec):
    return NOW - timedelta(seconds=sec)


@pytest.mark.parametrize("beats,expected,stale_keys,severity", [
    ({"w": ago(10)}, ["w"], [], None),
    ({"w": ago(900)}, ["w"], ["w"], "warning"),
    ({}, ["w"], ["w"], "critical"),                 # no heartbeat at all: unknown age is stale
    ({"w": None}, ["w"], ["w"], "critical"),
    ({"w": ago(-4000)}, ["w"], ["w"], "warning"),   # a beat from the far future is not trusted
    ({"other": ago(9999)}, [], [], None),           # workers that are not expected are ignored
    ({"a": ago(10), "b": ago(1000)}, ["a", "b"], ["b"], "warning"),
])
def test_heartbeats(beats, expected, stale_keys, severity):
    out = health.check_heartbeats(beats, expected, NOW, 300)
    assert [f.key for f in out] == stale_keys
    assert all(f.kind == "worker_heartbeat_stale" and f.severity == severity for f in out)


@pytest.mark.parametrize("open_,quote,bar,stale", [
    (True, 10, None, False),
    (True, 900, 20, False),      # a fresh bar is enough
    (True, 900, 900, True),
    (True, None, None, True),    # nothing stored at all while open: fail closed
    (False, 9999, 9999, False),
    (None, 9999, 9999, False),   # market state unknown: no alert
])
def test_market_data(open_, quote, bar, stale):
    assert bool(health.check_market_data(open_, quote, bar, 300)) is stale


@pytest.mark.parametrize("n,fires", [(0, False), (2, False), (3, True), (10, True)])
def test_order_failures(n, fires):
    out = health.check_order_failures(n, 60, 3)
    assert bool(out) is fires
    assert all(f.severity == "critical" for f in out)


@pytest.mark.parametrize("spend,budget,pct,expect", [
    (1.0, 5.0, 80, None),
    (4.0, 5.0, 80, "warning"),
    (5.0, 5.0, 80, "critical"),
    (9.0, 5.0, 80, "critical"),
    (4.9, None, 80, None),     # no budget configured
    (4.9, 0, 80, None),
])
def test_llm_budgets(spend, budget, pct, expect):
    out = health.check_llm_budgets({"research": (spend, budget)}, pct)
    assert [f.severity for f in out] == ([expect] if expect else [])
    if out:
        assert out[0].key == "research" and out[0].details["role"] == "research"


@pytest.mark.parametrize("counts,failed,kinds", [
    ({"queued": 1}, 0, []),
    ({"queued": 20, "leased": 5}, 0, ["research_backlog"]),
    ({"queued": 1}, 3, ["research_jobs_failed"]),
    ({"queued": 30}, 5, ["research_backlog", "research_jobs_failed"]),
    ({}, 0, []),
])
def test_research_queue(counts, failed, kinds):
    out = health.check_research_queue(counts, failed, 25, 3)
    assert [f.kind for f in out] == kinds


@pytest.mark.parametrize("mism,fires", [
    ({}, False),
    ({"reconcile_order_lag": 0}, False),
    ({"reconcile_order_missing": 1}, True),
    ({"reconcile_order_lag": 2, "reconcile_unknown_position": 1}, True),
])
def test_reconcile(mism, fires):
    out = health.check_reconcile(mism, 120, 1)
    assert bool(out) is fires
    if out:
        assert out[0].severity == "critical" and out[0].details["total"] == sum(mism.values())


@pytest.mark.parametrize("when,expected", [
    (datetime(2026, 1, 6, 15, 0, tzinfo=timezone.utc), True),    # Tue 10:00 ET
    (datetime(2026, 1, 6, 13, 0, tzinfo=timezone.utc), False),   # Tue 08:00 ET
    (datetime(2026, 1, 6, 21, 30, tzinfo=timezone.utc), False),  # Tue 16:30 ET
    (datetime(2026, 1, 10, 15, 0, tzinfo=timezone.utc), False),  # Saturday
    (datetime(2026, 7, 7, 14, 0, tzinfo=timezone.utc), True),    # summer time: 10:00 EDT
])
def test_market_open_approx(when, expected):
    assert health.market_open_approx(when) is expected


def test_check_error_is_a_warning():
    f = health.check_error("market_data", "boom")
    assert (f.kind, f.key, f.severity) == ("health_check_error", "market_data", "warning")
