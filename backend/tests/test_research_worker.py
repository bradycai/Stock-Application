"""Research worker: evidence bundles, analyst validation, failure isolation, ready/failed rules,
job queue behavior, budget pauses, no order calls, and the bundle API.
Needs TEST_DATABASE_URL (skipped otherwise). Claude, broker and market data are fakes."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import get_db
from app.deps import require_token
from app.models import (
    AnalystReportRow, Filing, LLMCall, ResearchBundle, ResearchJob, SignalEvent, WatcherDecision,
    WorkerHeartbeat,
)
from app.routers import research as research_router
from app.services import broker, data_aggregator, llm
from app.services.research import analysts, bundle as bundle_mod
from app.services.research.contracts import AnalystReport
from app.services.watcher import budget, jobs
from app.workers import research_worker as rw

NOW = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
ISO = NOW.isoformat()


@pytest.fixture()
def factory(pg_db):
    return sessionmaker(bind=pg_db.get_bind(), future=True)


@pytest.fixture(autouse=True)
def no_orders(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("research pipeline must never touch order functions")
    for fn in ("submit_bracket_order", "cancel_open_orders", "place_stop_order", "close_position",
               "cancel_open_buy_orders", "close_all_positions"):
        monkeypatch.setattr(broker, fn, boom)


def fake_context(symbol="AAPL", *, news=True, indicators=True, held=False, fundamentals=True,
                 macro=True) -> dict:
    entry = {
        "symbol": symbol, "last_price": 190.0, "intraday_change_pct": 1.2, "today_open": 188.0,
        "volume": 1000, "quote_ts": ISO, "last_trade_ts": ISO, "sector": "Technology",
        "indicators": ({"history_days": 250, "data_sufficient": True, "last_bar_ts": ISO, "last_close": 190.0,
                        "rsi_14": 55.0, "sma_50": 180.0, "macd": {"macd": 1.0, "signal": 0.5}}
                       if indicators else {"history_days": 1, "data_sufficient": False}),
        "fundamentals": ({"earnings_date": None, "analyst_actions": None,
                          "revenue_annual": {"value": 1e9, "filed_at": "2025-11-01"}, "last_filing": None}
                         if fundamentals else {"earnings_date": None, "analyst_actions": None}),
        "news": ([{"id": "news:123", "headline": "Acme beats", "summary": "s", "publisher": "wire",
                   "published_at": ISO, "event_type": "earnings", "is_new": True}] if news else []),
        "quality": {"stale": False, "incomplete": False, "missing": []},
    }
    positions = ([{"symbol": symbol, "qty": 10.0, "avg_entry_price": 150.0, "current_price": 190.0,
                   "market_value": 1900.0, "unrealized_pl": 400.0, "unrealized_plpc": 26.0}] if held else [])
    return {
        "portfolio": {"equity": 100000.0, "cash": 50000.0, "buying_power": 100000.0, "day_pnl": 0.0,
                      "day_pnl_pct": 0.0, "open_positions": len(positions), "is_paper": True},
        "positions": positions,
        "market": {"open": True, "clock": {}, "feed": "iex"},
        "market_context": {"indices": {"SPY": {"last": 500.0, "as_of": ISO}}, "regime_hint": "risk_on",
                           "sector_breadth_pct_above_sma50": 70.0},
        **({"macro": {"series": {"DGS10": {"value": 4.1, "observation_date": "2026-01-02"},
                                 "FEDFUNDS": None}}} if macro else {}),
        "watchlist_data": {symbol: entry},
        "constraints": {"max_position_pct": 5.0},
    }


@pytest.fixture()
def ctx_holder(monkeypatch):
    holder = {"ctx": fake_context()}
    monkeypatch.setattr(data_aggregator, "build_context",
                        lambda tickers, *, db=None, run_id=None: (holder["ctx"], {}))
    return holder


def good_report(symbol, analyst, ids=None, **over) -> AnalystReport:
    base = dict(symbol=symbol, analyst=analyst, stance="bullish", confidence=0.6, summary="ok",
                key_points=["p"], evidence_ids=ids if ids is not None else [f"quote:{symbol}"], risks=[],
                invalidation="below sma_50", data_gaps=[])
    base.update(over)
    return AnalystReport(**base)


class FakeLLM:
    """Replaces llm.call_structured; `overrides` maps analyst -> report or exception."""
    def __init__(self, monkeypatch, overrides=None):
        self.calls = []
        self.overrides = overrides or {}
        monkeypatch.setattr(llm, "call_structured", self)

    def __call__(self, *, role, prompt_name, system, user, output_model, max_tokens=None):
        analyst = prompt_name.removeprefix("research_")
        self.calls.append((analyst, role))
        o = self.overrides.get(analyst)
        if isinstance(o, Exception):
            raise o
        out = o or good_report("AAPL", analyst)
        return llm.LLMResult(output=out, meta={"prompt_version": "v1", "served_model": "fake-model"})


def make_job(db, symbol="AAPL", kind="INVESTIGATE", key="k1", **kw):
    jid = jobs.enqueue(db, kind=kind, symbol=symbol, job_key=key, payload={"urgency": "high"}, **kw)
    db.commit()
    return jid


def llm_error():
    return llm.LLMError("api_error", "boom", {})


def rows_by_analyst(db, bundle_id):
    return {r.analyst: r for r in db.scalars(select(AnalystReportRow).where(
        AnalystReportRow.bundle_id == bundle_id))}


# --------------------------------------------------------------------------- bundle
def test_bundle_evidence_ids_and_context(pg_db, ctx_holder):
    ctx_holder["ctx"] = fake_context(held=True)
    sig = SignalEvent(event_key="e1", symbol="AAPL", event_type="price_move", source="watcher",
                      occurred_at=NOW, payload={"pct": 3})
    pg_db.add(sig)
    pg_db.flush()
    dec = WatcherDecision(symbol="AAPL", action="INVESTIGATE", urgency="high", evidence_ids=[],
                          signal_event_ids=[sig.id], explanation="x")
    pg_db.add(dec)
    pg_db.flush()
    jid = make_job(pg_db, kind="REVIEW_POSITION", signal_event_ids=[sig.id], decision_id=dec.id)
    (job,) = jobs.lease(pg_db, "w", now=NOW)
    b = bundle_mod.build_bundle(pg_db, job, now=NOW)
    ids = set(b.evidence)
    assert {"quote:AAPL", "ind:AAPL.rsi_14", "ind:AAPL.macd", "news:123", "fund:AAPL.revenue_annual",
            "macro:DGS10", "regime:spy", "regime:summary", "pos:AAPL", f"signal:{sig.id}",
            f"decision:{dec.id}", "quality:AAPL"} <= ids
    assert "macro:FEDFUNDS" not in ids and "fund:AAPL.last_filing" not in ids  # nothing invented
    assert not any(i.startswith("ind:AAPL.data_sufficient") for i in ids)
    assert b.status == "collecting" and b.holding and b.job_id == jid
    ttl = timedelta(minutes=settings.research_bundle_ttl_minutes)
    assert b.market_snapshot_id and b.expires_at == NOW + ttl
    assert b.context["portfolio"]["equity"] == 100000.0
    assert b.context["positions"][0]["weight_pct"] == 1.9
    assert b.context["sector_exposure_pct"]
    assert "institutional" in b.context["data_gaps"]
    assert b.evidence["news:123"]["kind"] == "news" and b.evidence["quote:AAPL"]["source"] == "alpaca"


# --------------------------------------------------------------------------- validation
def _bundle(pg_db, ctx_holder, **ctx_kw):
    ctx_holder["ctx"] = fake_context(**ctx_kw)
    make_job(pg_db)
    (job,) = jobs.lease(pg_db, "w", now=NOW)
    return bundle_mod.build_bundle(pg_db, job, now=NOW)


def test_unknown_evidence_is_rejected(pg_db, ctx_holder, monkeypatch):
    b = _bundle(pg_db, ctx_holder)
    FakeLLM(monkeypatch, {"technical": good_report("AAPL", "technical", ["quote:AAPL", "news:999"])})
    row = analysts.run_analyst(pg_db, b, "technical")
    assert row.status == "rejected" and "unknown_evidence" in row.error
    assert row.report is not None  # raw output kept for audit


def test_symbol_and_analyst_mismatch_and_no_citation_rejected(pg_db, ctx_holder, monkeypatch):
    b = _bundle(pg_db, ctx_holder)
    fake = FakeLLM(monkeypatch, {"technical": good_report("MSFT", "technical")})
    assert "symbol_mismatch" in analysts.run_analyst(pg_db, b, "technical").error
    fake.overrides = {"news": good_report("AAPL", "technical")}
    assert "analyst_mismatch" in analysts.run_analyst(pg_db, b, "news").error
    fake.overrides = {"fundamental": good_report("AAPL", "fundamental", [])}
    assert analysts.run_analyst(pg_db, b, "fundamental").error == "no_evidence_cited"


def test_llm_error_stores_failed_and_exception_too(pg_db, ctx_holder, monkeypatch):
    b = _bundle(pg_db, ctx_holder)
    fake = FakeLLM(monkeypatch, {"technical": llm_error(), "news": RuntimeError("kaboom")})
    assert analysts.run_analyst(pg_db, b, "technical").status == "failed"
    assert analysts.run_analyst(pg_db, b, "news").status == "failed"
    fake.overrides = {}
    assert analysts.run_analyst(pg_db, b, "fundamental").status == "ok"


def test_no_relevant_data_stores_insufficient_without_llm_call(pg_db, ctx_holder, monkeypatch):
    b = _bundle(pg_db, ctx_holder)
    fake = FakeLLM(monkeypatch)
    row = analysts.run_analyst(pg_db, b, "institutional")
    assert row.status == "ok" and row.report["stance"] == "insufficient_data"
    assert row.report["data_gaps"] and row.report["evidence_ids"] == []
    assert fake.calls == []


def test_institutional_runs_when_filings_exist(pg_db, ctx_holder, monkeypatch):
    pg_db.add(Filing(symbol="AAPL", accession="0001-26-1", form="4", filed_at=NOW.date()))
    pg_db.flush()
    b = _bundle(pg_db, ctx_holder)
    assert "filing:0001-26-1" in b.evidence
    fake = FakeLLM(monkeypatch, {"institutional": good_report("AAPL", "institutional", ["filing:0001-26-1"])})
    assert analysts.run_analyst(pg_db, b, "institutional").status == "ok"
    assert fake.calls == [("institutional", "research")]


def test_skill_files_exist_and_forbid_invention():
    for spec in analysts.SPECS.values():
        text = analysts.system_prompt(spec)
        assert "Never recalculate" in text and "evidence_ids" in text and "invalidat" in text


# --------------------------------------------------------------------------- worker
def test_cycle_ready_bundle_completes_job(factory, pg_db, ctx_holder, monkeypatch):
    jid = make_job(pg_db)
    fake = FakeLLM(monkeypatch)
    w = rw.ResearchWorker(factory, now=lambda: NOW)
    s = w.run_cycle()
    assert s["status"] == "ok" and s["ready"] == 1 and s["jobs"] == 1
    pg_db.expire_all()
    b = pg_db.scalar(select(ResearchBundle))
    assert b.status == "ready" and b.ready_at is not None
    assert pg_db.get(ResearchJob, jid).status == "done"
    reports = rows_by_analyst(pg_db, b.id)
    assert set(reports) == set(rw.ANALYSTS)
    assert reports["institutional"].report["stance"] == "insufficient_data"
    assert [c[0] for c in fake.calls] == ["technical", "news", "fundamental", "market_regime"]
    hb = pg_db.get(WorkerHeartbeat, rw.WORKER)
    assert hb.status == "ok" and hb.details["last_cycle"]["ready"] == 1
    assert pg_db.scalar(select(func.count()).select_from(AnalystReportRow)) == 5


def test_failure_isolation_other_analysts_still_run(factory, pg_db, ctx_holder, monkeypatch):
    make_job(pg_db)
    FakeLLM(monkeypatch, {"news": llm_error(),
                          "market_regime": good_report("AAPL", "market_regime", ["regime:spy", "bogus:1"])})
    s = rw.ResearchWorker(factory, now=lambda: NOW).run_cycle()
    assert s["ready"] == 1
    pg_db.expire_all()
    b = pg_db.scalar(select(ResearchBundle))
    st = {a: r.status for a, r in rows_by_analyst(pg_db, b.id).items()}
    assert st == {"technical": "ok", "news": "failed", "fundamental": "ok", "market_regime": "rejected",
                  "institutional": "ok"}
    assert b.status == "ready"


def test_technical_failure_fails_bundle_and_retries_then_fails_job(factory, pg_db, ctx_holder, monkeypatch):
    jid = make_job(pg_db, key="k-retry", max_attempts=2)
    FakeLLM(monkeypatch, {"technical": llm_error()})
    w = rw.ResearchWorker(factory, now=lambda: NOW)
    assert w.run_cycle()["failed"] >= 1
    pg_db.expire_all()
    job = pg_db.get(ResearchJob, jid)
    assert job.status == "queued" and job.attempts == 1 and job.last_error
    assert pg_db.scalars(select(ResearchBundle.status)).all() == ["failed"]
    w.run_cycle()
    pg_db.expire_all()
    assert pg_db.get(ResearchJob, jid).status == "failed"  # bounded: no third attempt
    assert pg_db.scalar(select(func.count()).select_from(ResearchBundle)) == 2


def test_technical_insufficient_data_is_not_ready(factory, pg_db, ctx_holder, monkeypatch):
    ctx_holder["ctx"] = fake_context(indicators=False)
    make_job(pg_db)
    fake = FakeLLM(monkeypatch)
    rw.ResearchWorker(factory, now=lambda: NOW).run_cycle()
    pg_db.expire_all()
    assert pg_db.scalar(select(ResearchBundle.status)) == "failed"
    assert "technical" not in [c[0] for c in fake.calls]


def test_build_error_fails_job_not_worker(factory, pg_db, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("data down")
    monkeypatch.setattr(data_aggregator, "build_context", broken)
    jid = make_job(pg_db)
    s = rw.ResearchWorker(factory, now=lambda: NOW).run_cycle()
    assert s["errors"] == 1
    pg_db.expire_all()
    job = pg_db.get(ResearchJob, jid)
    assert job.status == "queued" and "data down" in job.last_error


def test_budget_exhausted_leases_nothing(factory, pg_db, ctx_holder, monkeypatch):
    jid = make_job(pg_db)
    monkeypatch.setattr(settings, "research_daily_budget_usd", 0.0)
    fake = FakeLLM(monkeypatch)
    s = rw.ResearchWorker(factory, now=lambda: NOW).run_cycle()
    assert s["status"] == "budget_paused" and s["budget_skip_reason"] == "daily_budget"
    pg_db.expire_all()
    job = pg_db.get(ResearchJob, jid)
    assert job.status == "queued" and job.attempts == 0 and fake.calls == []


def test_budget_hit_mid_job_releases_without_consuming_attempt(factory, pg_db, ctx_holder, monkeypatch):
    jid = make_job(pg_db)
    FakeLLM(monkeypatch)
    real = budget.check
    n = {"i": 0}

    def flaky(db, chars, role="watcher", now=None):
        n["i"] += 1
        if n["i"] >= 4:  # cycle gate + technical + news pass; the 4th check refuses
            return False, "monthly_budget", budget.status(db, role, now)
        return real(db, chars, role, now)
    monkeypatch.setattr(budget, "check", flaky)
    s = rw.ResearchWorker(factory, now=lambda: NOW).run_cycle()
    assert s["budget_released"] == 1 and s["status"] == "budget_paused"
    pg_db.expire_all()
    job = pg_db.get(ResearchJob, jid)
    assert job.status == "queued" and job.attempts == 0
    assert pg_db.scalar(select(ResearchBundle.status)) == "failed"


def test_budget_is_per_role(pg_db):
    pg_db.add(LLMCall(role="watcher", prompt_name="p", prompt_version="v", requested_model="m",
                      status="ok", cost_usd=1.5, created_at=NOW))
    pg_db.add(LLMCall(role="research", prompt_name="p", prompt_version="v", requested_model="m",
                      status="ok", cost_usd=0.25, created_at=NOW))
    pg_db.commit()
    assert budget.status(pg_db, "research", NOW).daily_spent == 0.25
    assert budget.status(pg_db, "research", NOW).daily_limit == settings.research_daily_budget_usd
    assert budget.status(pg_db, "watcher", NOW).daily_spent == 1.5


def test_stale_bundles_expire(factory, pg_db):
    old = ResearchBundle(symbol="A", kind="INVESTIGATE", status="ready", as_of=NOW, evidence={}, context={},
                         expires_at=NOW - timedelta(minutes=1))
    fresh = ResearchBundle(symbol="B", kind="INVESTIGATE", status="ready", as_of=NOW, evidence={},
                           context={}, expires_at=NOW + timedelta(minutes=30))
    done = ResearchBundle(symbol="C", kind="INVESTIGATE", status="strategized", as_of=NOW, evidence={},
                          context={}, expires_at=NOW - timedelta(minutes=1))
    pg_db.add_all([old, fresh, done])
    pg_db.commit()
    s = rw.ResearchWorker(factory, now=lambda: NOW).run_cycle()
    assert s["expired"] == 1
    pg_db.expire_all()
    assert [b.status for b in pg_db.scalars(select(ResearchBundle).order_by(ResearchBundle.id))] == [
        "expired", "ready", "strategized"]


def test_only_research_job_kinds_and_single_instance_lock(pg_db):
    from app.database import engine  # noqa: F401  (module import must work without side effects)
    assert rw.KINDS == ("INVESTIGATE", "REVIEW_POSITION")
    eng = pg_db.get_bind()
    first = rw.try_acquire_lock(eng)
    try:
        assert first is not None
        assert rw.try_acquire_lock(eng) is None
    finally:
        rw.release_lock(first)
    again = rw.try_acquire_lock(eng)
    assert again is not None
    rw.release_lock(again)


def test_disabled_by_default():
    assert settings.research_worker_enabled is False
    assert rw.main() == 0


# --------------------------------------------------------------------------- API
@pytest.fixture()
def client(pg_db):
    app = FastAPI()
    app.include_router(research_router.router)
    app.dependency_overrides[get_db] = lambda: pg_db
    app.dependency_overrides[require_token] = lambda: "user:test"
    return TestClient(app)


def test_bundle_api(client, factory, pg_db, ctx_holder, monkeypatch):
    make_job(pg_db)
    FakeLLM(monkeypatch)
    rw.ResearchWorker(factory, now=lambda: NOW).run_cycle()
    pg_db.expire_all()
    lst = client.get("/research/bundles").json()
    assert len(lst) == 1 and lst[0]["status"] == "ready" and lst[0]["symbol"] == "AAPL"
    assert lst[0]["reports"]["technical"]["stance"] == "bullish"
    assert client.get("/research/bundles?status=failed").json() == []
    assert len(client.get("/research/bundles?symbol=aapl&limit=5").json()) == 1
    one = client.get(f"/research/bundles/{lst[0]['id']}").json()
    assert "quote:AAPL" in one["evidence"] and len(one["analyst_reports"]) == 5
    assert one["context"]["portfolio"]["equity"] == 100000.0
    assert client.get("/research/bundles/99999").status_code == 404
