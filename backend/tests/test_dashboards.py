"""Research and LLM-usage dashboard endpoints (read-only)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.deps import require_token
from app.models import AnalysisRun, IntentAction, IntentStatus, LLMCall, MarketSnapshot, TradeIntent
from app.routers import research, usage


@pytest.fixture()
def client(pg_db):
    app = FastAPI()
    app.include_router(research.router)
    app.include_router(usage.router)
    app.dependency_overrides[get_db] = lambda: pg_db
    app.dependency_overrides[require_token] = lambda: "user:test"
    return TestClient(app)


def _seed_run(db):
    now = datetime.now(timezone.utc)
    snap = MarketSnapshot(
        purpose="analysis", as_of=now, feed="iex", symbols=["AAA", "BBB", "CCC"], data={"x": 1},
        content_hash="h",
        quality={"AAA": {"stale": True, "incomplete": False, "missing": [], "quote_age_sec": 900.0},
                 "BBB": {"stale": False, "incomplete": True, "missing": ["price_history"],
                         "quote_age_sec": 3.0},
                 "CCC": {"stale": False, "incomplete": False, "missing": [], "quote_age_sec": 1.0}})
    db.add(snap)
    db.flush()
    run = AnalysisRun(model="m", market_snapshot_id=snap.id, raw_response={},
                      input_context={"effort": "high", "prompt_version": "v1", "estimated_cost_usd": 0.12,
                                     "tickers_screened": 3, "batches": 2, "failed_batches": 1,
                                     "data_age_sec": 4.5})
    db.add(run)
    db.flush()
    for st in (IntentStatus.pending_approval, IntentStatus.pending_approval, IntentStatus.rejected_by_risk):
        db.add(TradeIntent(run_id=run.id, ticker="AAA", action=IntentAction.buy, status=st))
    db.commit()
    return run, snap


def test_runs_list_and_quality(client, pg_db):
    run, snap = _seed_run(pg_db)
    body = client.get("/research/runs?limit=5").json()
    assert len(body) == 1
    r = body[0]
    assert r["id"] == run.id and r["effort"] == "high" and r["failed_batches"] == 1
    assert r["quality"] == {"stale": 1, "incomplete": 1, "symbols": 3}
    assert r["intents_by_status"] == {"pending_approval": 2, "rejected_by_risk": 1}
    q = client.get(f"/research/runs/{run.id}/quality").json()
    assert q["market_snapshot_id"] == snap.id
    by = {s["symbol"]: s for s in q["symbols"]}
    assert by["AAA"]["stale"] and by["BBB"]["incomplete"] and by["BBB"]["missing"] == ["price_history"]
    assert client.get("/research/runs/9999/quality").status_code == 404


def test_run_without_snapshot(client, pg_db):
    run = AnalysisRun(model="m", raw_response={}, input_context={})
    pg_db.add(run)
    pg_db.commit()
    assert client.get("/research/runs").json()[0]["quality"] is None
    assert client.get(f"/research/runs/{run.id}/quality").json()["symbols"] == []


def _call(**kw):
    base = dict(role="strategist", prompt_name="p", prompt_version="v", requested_model="m1",
                status="ok", input_tokens=100, output_tokens=50, cache_read_tokens=10,
                cache_write_tokens=5, cost_usd=0.5)
    base.update(kw)
    return LLMCall(**base)


def test_usage_and_calls(client, pg_db):
    pg_db.add_all([_call(), _call(), _call(status="api_error", cost_usd=None, error="x" * 1000),
                   _call(role="watcher", requested_model="m2"), _call()])
    old = _call()
    old.created_at = datetime.now(timezone.utc) - timedelta(days=30)
    pg_db.add(old)
    pg_db.commit()
    u = client.get("/llm/usage?days=7").json()
    assert u["estimated"] is True
    t = u["totals"]
    assert t["calls"] == 5 and t["failures"] == 1
    assert t["input_tokens"] == 500 and t["cache_read_tokens"] == 50
    assert t["estimated_cost_usd"] == pytest.approx(2.0)
    assert {g["status"] for g in u["groups"]} == {"ok", "api_error"}
    assert len(client.get("/llm/calls?limit=3").json()) == 3
    failed = [c for c in client.get("/llm/calls").json() if c["status"] == "api_error"][0]
    assert len(failed["error"]) <= 301
    assert "prompt" not in failed and "prompt_text" not in failed


def test_usage_empty(client):
    u = client.get("/llm/usage").json()
    assert u["totals"]["calls"] == 0 and u["groups"] == []
    assert client.get("/llm/calls").json() == []
