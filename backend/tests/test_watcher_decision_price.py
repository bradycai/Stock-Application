"""/watcher/decisions price fields: point in time, null when unknown, batched."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.database import get_db
from app.deps import require_token
from app.models import MarketBar, SignalEvent, WatcherDecision
from app.routers import watcher as watcher_router

T = datetime(2026, 1, 6, 15, 30, tzinfo=timezone.utc)


@pytest.fixture()
def client(pg_db):
    app = FastAPI()
    app.include_router(watcher_router.router)
    app.dependency_overrides[get_db] = lambda: pg_db
    app.dependency_overrides[require_token] = lambda: "user:test"
    return TestClient(app)


def decision(db, symbol="AAPL", created=T, signals=()):
    d = WatcherDecision(symbol=symbol, action="WATCH", urgency="low", evidence_ids=[],
                        signal_event_ids=list(signals), created_at=created)
    db.add(d)
    db.flush()
    return d


def signal(db, key, payload, *, symbol="AAPL", etype="LARGE_MOVE", occurred=T, created=None):
    e = SignalEvent(event_key=key, symbol=symbol, event_type=etype, source="quote", occurred_at=occurred,
                    created_at=created or occurred, payload=payload)
    db.add(e)
    db.flush()
    return e.id


def bar(db, ts, close, symbol="AAPL"):
    db.add(MarketBar(symbol=symbol, timeframe="1Min", ts=ts, open=close, high=close, low=close,
                     close=close, volume=1, feed="iex"))
    db.flush()


def by_id(client):
    return {d["id"]: d for d in client.get("/watcher/decisions").json()}


def test_no_price_data_is_null(client, pg_db):
    d = decision(pg_db)
    got = by_id(client)[d.id]
    assert got["last_price"] is None and got["price_change_pct"] is None


def test_price_and_change_from_signal_events(client, pg_db):
    sid = signal(pg_db, "k1", {"price": 105.0, "reference_price": 100.0, "change_pct": 5.0})
    d = decision(pg_db, signals=[sid])
    got = by_id(client)[d.id]
    assert got["last_price"] == 105.0 and got["price_change_pct"] == 5.0


def test_bar_signal_gives_price_without_change(client, pg_db):
    sid = signal(pg_db, "k2", {"close": 51.5, "range_high": 50}, etype="BREAKOUT")
    d = decision(pg_db, signals=[sid])
    got = by_id(client)[d.id]
    assert got["last_price"] == 51.5 and got["price_change_pct"] is None


def test_falls_back_to_last_completed_bar(client, pg_db):
    bar(pg_db, T - timedelta(minutes=5), 99.0)
    bar(pg_db, T - timedelta(minutes=1), 101.0)
    bar(pg_db, T - timedelta(seconds=30), 777.0)  # not complete at decision time
    d = decision(pg_db)
    got = by_id(client)[d.id]
    assert got["last_price"] == 101.0 and got["price_change_pct"] is None


def test_never_uses_data_after_the_decision(client, pg_db):
    # a later bar, a later signal (even one linked to the decision) and another symbol's bar
    bar(pg_db, T + timedelta(minutes=2), 500.0)
    bar(pg_db, T - timedelta(minutes=3), 100.0)
    bar(pg_db, T - timedelta(minutes=1), 900.0, symbol="MSFT")
    later = signal(pg_db, "late", {"price": 300.0, "change_pct": 50.0},
                   occurred=T + timedelta(minutes=1), created=T + timedelta(minutes=1))
    d = decision(pg_db, signals=[later])
    got = by_id(client)[d.id]
    assert got["last_price"] == 100.0       # the pre-decision bar, not 500 / 300
    assert got["price_change_pct"] is None  # the post-decision signal's 50% is ignored


def test_signal_created_after_decision_is_ignored_even_if_it_occurred_before(client, pg_db):
    late_row = signal(pg_db, "late2", {"price": 300.0, "change_pct": 9.0},
                      occurred=T - timedelta(minutes=1), created=T + timedelta(seconds=5))
    d = decision(pg_db, signals=[late_row])
    got = by_id(client)[d.id]
    assert got["last_price"] is None and got["price_change_pct"] is None


def test_stale_bar_is_not_used(client, pg_db):
    bar(pg_db, T - timedelta(days=3), 42.0)
    d = decision(pg_db)
    assert by_id(client)[d.id]["last_price"] is None


def test_lookups_are_batched(client, pg_db):
    sids = []
    for i in range(30):
        sids.append(signal(pg_db, f"b{i}", {"price": 10.0 + i, "change_pct": 1.0}))
    for i in range(30):
        decision(pg_db, signals=[sids[i]])
    for i in range(20):
        decision(pg_db, symbol="MSFT")
    bar(pg_db, T - timedelta(minutes=2), 7.0, symbol="MSFT")
    statements = []
    eng = pg_db.get_bind()

    def count(conn, cursor, statement, *a):
        statements.append(statement)

    event.listen(eng, "before_cursor_execute", count)
    try:
        r = client.get("/watcher/decisions?limit=50")
    finally:
        event.remove(eng, "before_cursor_execute", count)
    assert len(r.json()) == 50
    assert len(statements) <= 4
    msft = [d for d in r.json() if d["symbol"] == "MSFT"]
    assert msft and all(d["last_price"] == 7.0 for d in msft)
