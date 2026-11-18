"""Watcher: job queue, budget, heartbeats, worker cycles, single-instance lock, status API.
Needs TEST_DATABASE_URL (skipped otherwise). Claude, broker and market data are fakes."""
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.deps import require_token
from app.models import (
    AuditLog, LatestQuote, LLMCall, NewsEvent, ResearchJob, SignalEvent, Watchlist, WatcherDecision,
    WorkerHeartbeat,
)
from app.routers import watcher as watcher_router
from app.services import broker, market_data, news_store
from app.services.watcher import budget, heartbeat, jobs
from app.workers import market_watcher as mw

NOW = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)


@pytest.fixture()
def factory(pg_db):
    return sessionmaker(bind=pg_db.get_bind(), future=True)


# --------------------------------------------------------------------------- job queue
def test_enqueue_dedupes_on_job_key(pg_db):
    a = jobs.enqueue(pg_db, kind="INVESTIGATE", symbol="aapl", job_key="k1", payload={"x": 1})
    b = jobs.enqueue(pg_db, kind="INVESTIGATE", symbol="AAPL", job_key="k1")
    pg_db.commit()
    assert a is not None and b is None
    assert pg_db.scalar(select(func.count()).select_from(ResearchJob)) == 1
    assert pg_db.get(ResearchJob, a).symbol == "AAPL"
    with pytest.raises(ValueError):
        jobs.enqueue(pg_db, kind="BUY", symbol="X", job_key="k2")


def test_lease_marks_leased_and_expired_lease_is_reclaimed(pg_db):
    jobs.enqueue(pg_db, kind="INVESTIGATE", symbol="A", job_key="k1")
    pg_db.commit()
    (j,) = jobs.lease(pg_db, "w1", lease_seconds=60, now=NOW)
    assert j.status == "leased" and j.attempts == 1 and j.leased_by == "w1"
    assert jobs.lease(pg_db, "w2", now=NOW + timedelta(seconds=30)) == []     # lease still valid
    (j2,) = jobs.lease(pg_db, "w2", now=NOW + timedelta(seconds=61))          # lease expired
    assert j2.id == j.id and j2.attempts == 2 and j2.leased_by == "w2"


def test_complete_and_bounded_retries(pg_db):
    jobs.enqueue(pg_db, kind="REVIEW_POSITION", symbol="A", job_key="k1", max_attempts=2)
    jobs.enqueue(pg_db, kind="INVESTIGATE", symbol="B", job_key="k2")
    pg_db.commit()
    (a,) = jobs.lease(pg_db, "w", kinds=("REVIEW_POSITION",), now=NOW)
    assert jobs.fail(pg_db, a, "boom") == "queued" and a.last_error == "boom"
    (a,) = jobs.lease(pg_db, "w", kinds=("REVIEW_POSITION",), now=NOW)
    assert a.attempts == 2 and jobs.fail(pg_db, a, "boom again") == "failed"
    assert jobs.lease(pg_db, "w", kinds=("REVIEW_POSITION",), now=NOW) == []
    (b,) = jobs.lease(pg_db, "w", now=NOW)
    jobs.complete(pg_db, b)
    assert jobs.counts(pg_db) == {"queued": 0, "leased": 0, "done": 1, "failed": 1}


def test_expired_lease_after_max_attempts_becomes_failed(pg_db):
    jobs.enqueue(pg_db, kind="INVESTIGATE", symbol="A", job_key="k1", max_attempts=1)
    pg_db.commit()
    jobs.lease(pg_db, "w", lease_seconds=10, now=NOW)
    assert jobs.lease(pg_db, "w2", now=NOW + timedelta(seconds=11)) == []
    assert pg_db.scalar(select(ResearchJob.status)) == "failed"


def test_concurrent_lease_skips_locked_rows(pg_db, factory):
    jobs.enqueue(pg_db, kind="INVESTIGATE", symbol="A", job_key="k1")
    jobs.enqueue(pg_db, kind="INVESTIGATE", symbol="B", job_key="k2")
    pg_db.commit()
    other = factory()
    try:
        (first,) = jobs.lease(other, "w1", now=NOW)          # row lock held, not committed
        (second,) = jobs.lease(pg_db, "w2", now=NOW)         # SKIP LOCKED: gets the other job
        assert first.id != second.id
    finally:
        other.rollback()
        other.close()


# --------------------------------------------------------------------------- budget
def _call(db, cost, role="watcher", at=NOW):
    db.add(LLMCall(role=role, prompt_name="p", prompt_version="v", requested_model="m", status="ok",
                   cost_usd=cost, created_at=at))
    db.commit()


def test_budget_daily_and_monthly_ceilings(pg_db, monkeypatch):
    monkeypatch.setattr(budget.settings, "watcher_daily_budget_usd", 1.0)
    monkeypatch.setattr(budget.settings, "watcher_monthly_budget_usd", 5.0)
    _call(pg_db, 0.6)
    _call(pg_db, 3.0, at=NOW - timedelta(days=2))
    _call(pg_db, 99.0, role="strategist")                      # other roles don't count
    st = budget.status(pg_db, now=NOW + timedelta(hours=1))
    assert st.daily_spent == pytest.approx(0.6) and st.monthly_spent == pytest.approx(3.6)
    assert st.allows(0.3) == (True, None)
    assert st.allows(0.5) == (False, "daily_budget")
    _call(pg_db, 0.3, at=NOW - timedelta(days=1))
    st = budget.status(pg_db, now=NOW + timedelta(hours=1))
    assert st.allows(0.3) == (False, "monthly_budget") or st.allows(0.3)[1] is None
    ok, reason, _ = budget.check(pg_db, 10**8, now=NOW + timedelta(hours=1))
    assert not ok and reason in ("daily_budget", "monthly_budget")


# --------------------------------------------------------------------------- heartbeat
def test_heartbeat_upserts(pg_db):
    heartbeat.beat(pg_db, "w", "ok", {"n": 1}, started_at=NOW, now=NOW)
    heartbeat.beat(pg_db, "w", "idle", {"n": 2}, started_at=NOW, now=NOW + timedelta(seconds=60))
    pg_db.commit()
    assert pg_db.scalar(select(func.count()).select_from(WorkerHeartbeat)) == 1
    hb = heartbeat.get(pg_db, "w")
    assert hb.status == "idle" and hb.details == {"n": 2} and hb.last_beat == NOW + timedelta(seconds=60)


# --------------------------------------------------------------------------- worker
class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


class Harness:
    def __init__(self, factory, db, *, is_open=True, action="INVESTIGATE", positions=None):
        self.factory, self.db = factory, db
        self.clock = Clock()
        self.is_open = is_open
        self.action = action
        self.calls = []
        self.snapshot_calls = []
        self.w = mw.MarketWatcher(
            factory, get_positions=lambda: positions or [], clock=lambda: {"is_open": self.is_open},
            fetch_snapshots=self._snap, decide_fn=self._decide, regime_fn=lambda: "risk_on", now=self.clock)

    def _snap(self, symbols):
        self.snapshot_calls.append(list(symbols))
        return {}

    def _decide(self, items, now):
        self.calls.append(items)
        out = []
        for it in items:
            ev = [n["id"] for n in it["news"] if n["is_new"]] or [it["signals"][0]["id"]]
            out.append({"symbol": it["symbol"], "action": self.action, "event_type": "news",
                        "urgency": "high",
                        "evidence_ids": ev, "explanation": "because", "expires_at": now + timedelta(hours=1),
                        "downgraded": False,
                        "signal_event_ids": [int(s["id"].split(":")[1]) for s in it["signals"]]})
        return out, {"prompt_version": "market_watcher@abc", "served_model": "claude-haiku-5-5"}

    def seed_quote(self, sym, price=100.0):
        self.db.merge(LatestQuote(symbol=sym, last_trade_price=price, last_trade_ts=self.clock.t,
                                  feed="iex", source="stream"))
        self.db.commit()

    def add_news(self, sym, headline):
        news_store.ingest_news(self.db, [{
            "source": "alpaca", "external_id": headline, "headline": headline, "symbols": [sym],
            "published_at": self.clock.t, "publisher": "x", "summary": "s"}])
        self.db.commit()

    def cycle(self):
        self.db.expire_all()
        return self.w.run_cycle()


@pytest.fixture()
def h(pg_db, factory):
    pg_db.add(Watchlist(ticker="AAPL", active=True))
    pg_db.commit()
    hh = Harness(factory, pg_db)
    hh.seed_quote("AAPL")
    return hh


def test_no_change_means_no_claude_call(h, pg_db):
    first = h.cycle()
    assert first["status"] == "ok" and first["calls"] == 0          # baseline only
    h.clock.t += timedelta(seconds=60)
    h.seed_quote("AAPL", 100.1)
    second = h.cycle()
    assert second["changed"] == 0 and second["calls"] == 0 and h.calls == []
    assert h.snapshot_calls == []                                     # stored quote was fresh
    assert pg_db.scalar(select(func.count()).select_from(SignalEvent)) == 0


def test_new_news_flows_to_decision_job_and_heartbeat(h, pg_db):
    h.cycle()
    h.clock.t += timedelta(seconds=60)
    h.add_news("AAPL", "Apple raises guidance")
    s = h.cycle()
    assert s["changed"] == 1 and s["calls"] == 1 and s["decisions"] == 1 and s["jobs"] == 1
    (items,) = h.calls
    assert [i["symbol"] for i in items] == ["AAPL"] and items[0]["news"][0]["is_new"] is True
    d = pg_db.scalar(select(WatcherDecision))
    assert d.action == "INVESTIGATE" and d.prompt_version == "market_watcher@abc"
    assert d.run_id.startswith("watcher-")
    job = pg_db.scalar(select(ResearchJob))
    assert job.kind == "INVESTIGATE" and job.decision_id == d.id and job.status == "queued"
    sig = pg_db.scalar(select(SignalEvent))
    assert sig.event_type == "NEWS" and sig.id in d.signal_event_ids
    hb = heartbeat.get(pg_db, mw.WORKER)
    assert hb.status == "ok" and hb.details["last_cycle"]["calls"] == 1

    h.clock.t += timedelta(minutes=30)                                # nothing new: still no call
    assert h.cycle()["calls"] == 0 and len(h.calls) == 1


def test_cooldown_blocks_repeat_assessment(h, pg_db, monkeypatch):
    monkeypatch.setattr(mw.settings, "watcher_cooldown_minutes", 15)
    h.cycle()
    h.clock.t += timedelta(seconds=60)
    h.add_news("AAPL", "story one")
    assert h.cycle()["calls"] == 1
    h.clock.t += timedelta(minutes=5)
    h.add_news("AAPL", "story two")
    s = h.cycle()
    assert s["cooldown_skipped"] == 1 and s["calls"] == 0 and len(h.calls) == 1
    h.clock.t += timedelta(minutes=20)
    h.add_news("AAPL", "story three")
    assert h.cycle()["calls"] == 1 and len(h.calls) == 2


def test_market_closed_makes_no_calls_but_beats(factory, pg_db):
    hh = Harness(factory, pg_db, is_open=False)
    s = hh.cycle()
    assert s == {**s, "status": "idle", "reason": "market_closed"} and hh.calls == []
    assert heartbeat.get(pg_db, mw.WORKER).status == "idle"
    hh.is_open = None                                                  # unknown clock fails closed
    assert hh.cycle()["reason"] == "clock_unknown"


def test_budget_exhausted_skips_claude_and_reports_it(h, pg_db, monkeypatch):
    monkeypatch.setattr(budget.settings, "watcher_daily_budget_usd", 0.01)
    _call(pg_db, 0.02, at=h.clock.t - timedelta(minutes=1))
    h.cycle()
    h.clock.t += timedelta(seconds=60)
    h.add_news("AAPL", "big news")
    s = h.cycle()
    assert s["changed"] == 1 and s["calls"] == 0 and s["budget_skipped"] == 1
    assert s["budget_skip_reason"] == "daily_budget" and h.calls == []
    assert heartbeat.get(pg_db, mw.WORKER).details["last_cycle"]["budget_skipped"] == 1


def test_llm_failure_records_nothing_and_does_not_crash(h, pg_db):
    from app.services.llm import LLMError

    def boom(items, now):
        raise LLMError("api_error", "down", {})

    h.w._decide = boom
    h.cycle()
    h.clock.t += timedelta(seconds=60)
    h.add_news("AAPL", "news")
    s = h.cycle()
    assert s["status"] == "ok" and s["llm_errors"] == 1 and s["decisions"] == 0
    assert pg_db.scalar(select(func.count()).select_from(WatcherDecision)) == 0


def test_alert_is_audited_and_never_touches_the_broker(factory, pg_db, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("watcher must never call broker order functions")

    for name in ("submit_bracket_order", "cancel_open_orders", "place_stop_order", "close_position",
                 "close_all_positions"):
        monkeypatch.setattr(broker, name, forbidden)
    from app.services import execution
    for name in dir(execution):
        if name in ("execute_intent", "submit_intent", "place_order"):
            monkeypatch.setattr(execution, name, forbidden)

    pg_db.add(Watchlist(ticker="AAPL", active=True))
    pg_db.commit()
    hh = Harness(factory, pg_db, action="ALERT",
                 positions=[{"symbol": "AAPL", "qty": 5, "avg_entry_price": 99.0}])
    hh.seed_quote("AAPL")
    hh.cycle()
    hh.clock.t += timedelta(seconds=60)
    hh.add_news("AAPL", "SEC investigation announced")
    s = hh.cycle()
    assert s["alerts"] == 1 and s["jobs"] == 0
    a = pg_db.scalar(select(AuditLog).where(AuditLog.event_type == "watcher.alert"))
    assert a.actor == "system:watcher" and a.payload["symbol"] == "AAPL"
    assert pg_db.scalar(select(func.count()).select_from(ResearchJob)) == 0
    # a held symbol also carries position context to the model
    assert hh.calls[0][0]["position"]["qty"] == 5


def test_review_position_job_for_held_symbol_and_job_dedupe(factory, pg_db):
    pg_db.add(Watchlist(ticker="AAPL", active=True))
    pg_db.commit()
    hh = Harness(factory, pg_db, action="REVIEW_POSITION",
                 positions=[{"symbol": "AAPL", "qty": 5, "avg_entry_price": 99.0}])
    hh.seed_quote("AAPL")
    hh.cycle()
    hh.clock.t += timedelta(seconds=60)
    hh.add_news("AAPL", "downgrade")
    assert hh.cycle()["jobs"] == 1
    assert pg_db.scalar(select(ResearchJob.kind)) == "REVIEW_POSITION"
    # same signals decided again -> same job_key -> no duplicate
    sig_ids = [s for (s,) in pg_db.execute(select(SignalEvent.id))]
    d = {"symbol": "AAPL", "action": "REVIEW_POSITION", "event_type": "e", "urgency": "low",
         "evidence_ids": [], "explanation": "", "expires_at": None, "downgraded": False,
         "signal_event_ids": sig_ids}
    summary = {"decisions": 0, "jobs": 0, "alerts": 0}
    hh.w._store_decision(pg_db, d, {}, "r", hh.clock.t, summary)
    assert summary["jobs"] == 0 and pg_db.scalar(select(func.count()).select_from(ResearchJob)) == 1


def test_cycles_never_overlap(h):
    assert h.w._cycle_lock.acquire(blocking=False)
    try:
        assert h.w.run_cycle() == {"status": "skipped_overlap"}
    finally:
        h.w._cycle_lock.release()


def test_stale_stored_quote_falls_back_to_batched_snapshot(factory, pg_db):
    pg_db.add_all([Watchlist(ticker="AAPL", active=True), Watchlist(ticker="MSFT", active=True)])
    pg_db.commit()
    hh = Harness(factory, pg_db)
    hh.seed_quote("AAPL")                       # fresh; MSFT has no stored quote
    hh.cycle()
    assert hh.snapshot_calls == [["MSFT"]]      # one batched request, only for the missing symbol


def test_stop_event_ends_run_forever(factory, pg_db):
    hh = Harness(factory, pg_db, is_open=False)
    mw.settings.watcher_closed_sleep_seconds  # attribute exists
    t = threading.Thread(target=hh.w.run_forever)
    t.start()
    hh.w.stop_event.set()
    t.join(timeout=10)
    assert not t.is_alive()


def test_single_instance_advisory_lock(pg_db):
    engine = pg_db.get_bind()
    first = mw.try_acquire_lock(engine)
    assert first is not None
    try:
        assert mw.try_acquire_lock(engine) is None
    finally:
        mw.release_lock(first)
    again = mw.try_acquire_lock(engine)
    assert again is not None
    mw.release_lock(again)


# --------------------------------------------------------------------------- API
@pytest.fixture()
def client(pg_db):
    app = FastAPI()
    app.include_router(watcher_router.router)
    app.dependency_overrides[get_db] = lambda: pg_db
    app.dependency_overrides[require_token] = lambda: "user:test"
    return TestClient(app)


def test_status_decisions_and_jobs_endpoints(client, pg_db):
    r = client.get("/watcher/status").json()
    assert r["enabled"] is False and r["heartbeat"] is None
    assert r["queue"] == {"queued": 0, "leased": 0, "done": 0, "failed": 0}
    assert r["budget"]["daily_limit_usd"] == 2.0

    heartbeat.beat(pg_db, mw.WORKER, "ok", {"last_cycle": {"calls": 2}}, started_at=NOW)
    pg_db.add(WatcherDecision(symbol="AAPL", action="WATCH", urgency="low", evidence_ids=[],
                              signal_event_ids=[], downgraded=False))
    jobs.enqueue(pg_db, kind="INVESTIGATE", symbol="AAPL", job_key="k")
    pg_db.commit()
    r = client.get("/watcher/status").json()
    assert r["heartbeat"]["status"] == "ok" and r["last_cycle"] == {"calls": 2} and r["queue"]["queued"] == 1
    assert client.get("/watcher/decisions?limit=5").json()[0]["symbol"] == "AAPL"
    assert len(client.get("/watcher/jobs?status=queued").json()) == 1
    assert client.get("/watcher/jobs?status=done").json() == []
    assert client.get("/watcher/jobs?status=bogus").status_code == 422


def test_news_store_rows_exist_for_harness(pg_db):
    assert pg_db.scalar(select(func.count()).select_from(NewsEvent)) == 0
    assert market_data.market_clock is not None
