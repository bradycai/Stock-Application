"""Candidate scanner: exclusions, ranking on stored bars, per-day dedup and the daily cap.
Stored data only; no Claude, broker or network."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import MarketBar, PositionManagement, ResearchJob, Watchlist
from app.services import scanner, universes

NOW = datetime(2026, 1, 6, 15, 0, tzinfo=timezone.utc)  # Tue 10:00 ET
SYMS = ["IONQ", "RGTI", "QBTS", "QUBT", "ARQQ"]  # the "Quantum/speculative" universe


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "scanner_universes", "Quantum/speculative")
    monkeypatch.setattr(settings, "scanner_top_n", 2)
    monkeypatch.setattr(settings, "scanner_max_jobs_per_day", 10)


def seed_bars(db, symbol, slope, n=70):
    for i in range(n):
        px = 20 + slope * i
        db.add(MarketBar(symbol=symbol, timeframe="1Day", ts=NOW - timedelta(days=n - i), open=px,
                         high=px + 1, low=px - 1, close=px, volume=1000 + i, feed="iex"))


def seed_all(db):
    for s, slope in zip(SYMS, (0.5, 0.4, 0.3, 0.2, 0.1)):
        seed_bars(db, s, slope)
    db.commit()


def keys(db):
    return sorted(db.scalars(select(ResearchJob.job_key)))


def test_universe_symbols_respects_setting(monkeypatch):
    assert scanner.universe_symbols() == SYMS
    monkeypatch.setattr(settings, "scanner_universes", "")
    assert set(SYMS) <= set(scanner.universe_symbols())
    assert len(scanner.universe_symbols()) == len(set(scanner.universe_symbols()))
    assert set(scanner.universe_symbols()) == {s for u in universes.UNIVERSES.values() for s in u}


def test_enqueues_top_n_investigate_jobs_with_day_key(pg_db):
    seed_all(pg_db)
    out = scanner.scan(pg_db, NOW)
    pg_db.commit()
    assert len(out["enqueued"]) == 2
    jobs = list(pg_db.scalars(select(ResearchJob)))
    assert {j.kind for j in jobs} == {"INVESTIGATE"}
    assert all(j.job_key == f"scan:2026-01-06:{j.symbol}" for j in jobs)
    assert all(j.payload["source"] == "scanner" for j in jobs)


def test_watchlist_and_held_symbols_are_excluded(pg_db):
    seed_all(pg_db)
    pg_db.add(Watchlist(ticker="IONQ", active=True))
    pg_db.add(Watchlist(ticker="RGTI", active=False))  # inactive: still a candidate
    pg_db.add(PositionManagement(symbol="QBTS", is_open=True, entry_price=1, original_qty=1, highest_price=1))
    pg_db.add(PositionManagement(symbol="QUBT", is_open=False, entry_price=1, original_qty=1,
                                 highest_price=1))
    pg_db.commit()
    monkey_top = settings.scanner_top_n
    assert monkey_top == 2
    out = scanner.scan(pg_db, NOW)
    syms = {e["symbol"] for e in out["enqueued"]}
    assert syms.isdisjoint({"IONQ", "QBTS"})
    assert out["candidates"] == 3


def test_rescan_same_day_dedups_and_takes_next_symbols(pg_db):
    seed_all(pg_db)
    first = {e["symbol"] for e in scanner.scan(pg_db, NOW)["enqueued"]}
    pg_db.commit()
    second = {e["symbol"] for e in scanner.scan(pg_db, NOW + timedelta(minutes=30))["enqueued"]}
    pg_db.commit()
    assert len(second) == 2 and first.isdisjoint(second)
    assert len(keys(pg_db)) == 4
    # next ET day starts fresh keys
    third = scanner.scan(pg_db, NOW + timedelta(days=1))
    assert third["day"] == "2026-01-07" and len(third["enqueued"]) == 2


def test_daily_cap_is_never_exceeded(pg_db, monkeypatch):
    seed_all(pg_db)
    monkeypatch.setattr(settings, "scanner_max_jobs_per_day", 3)
    monkeypatch.setattr(settings, "scanner_top_n", 2)
    a = scanner.scan(pg_db, NOW)
    pg_db.commit()
    b = scanner.scan(pg_db, NOW + timedelta(minutes=30))
    pg_db.commit()
    c = scanner.scan(pg_db, NOW + timedelta(minutes=60))
    pg_db.commit()
    assert (len(a["enqueued"]), len(b["enqueued"]), len(c["enqueued"])) == (2, 1, 0)
    assert c["skipped"] == "daily_cap_reached"
    assert len(keys(pg_db)) == 3


def test_symbols_without_history_are_not_enqueued(pg_db):
    seed_bars(pg_db, "IONQ", 0.5)
    seed_bars(pg_db, "RGTI", 0.1, n=1)  # one bar: insufficient
    pg_db.commit()
    out = scanner.scan(pg_db, NOW)
    assert [e["symbol"] for e in out["enqueued"]] == ["IONQ"]


def test_empty_database_is_a_clean_noop(pg_db):
    out = scanner.scan(pg_db, NOW)
    assert out["enqueued"] == []
    assert keys(pg_db) == []


def test_scanner_job_is_off_unless_enabled(pg_db, monkeypatch):
    from app.services.scheduler import runner
    monkeypatch.setattr(settings, "scanner_enabled", False)
    scan_def = next(j for j in runner.job_defs() if j.name == "scanner")
    assert scan_def.enabled() is False
