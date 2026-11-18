"""DB-backed ops tests: alert dedup / auto-resolve / ack, the health watcher cycle, the API,
retention, and recovery. No broker, Claude or network calls."""
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
    AuditLog, LLMCall, MarketBar, ResearchBundle, ResearchJob, SystemAlert, WorkerHeartbeat,
)
from app.routers import ops as ops_router
from app.services.ops import alerts, collect, health, retention
from app.services.watcher import heartbeat, jobs
from app.workers.health_watcher import HealthWatcher

# Tuesday 10:00 ET, market open
NOW = datetime(2026, 1, 6, 15, 0, tzinfo=timezone.utc)


@pytest.fixture()
def factory(pg_db):
    return sessionmaker(bind=pg_db.get_bind(), future=True)


@pytest.fixture()
def client(pg_db):
    app = FastAPI()
    app.include_router(ops_router.router)
    app.dependency_overrides[get_db] = lambda: pg_db
    app.dependency_overrides[require_token] = lambda: "user:test"
    return TestClient(app)


def F(kind="k", key="a", sev="warning", msg="m"):
    return health.Finding(kind, key, sev, msg, {"x": 1})


# --------------------------------------------------------------------------- alert store
def test_dedup_counts_and_last_seen(pg_db):
    a = alerts.raise_alert(pg_db, F(), NOW)
    b = alerts.raise_alert(pg_db, F(sev="critical"), NOW + timedelta(minutes=1))
    pg_db.commit()
    assert a.id == b.id
    assert b.count == 2 and b.severity == "critical"
    assert b.first_seen == NOW and b.last_seen == NOW + timedelta(minutes=1)
    alerts.raise_alert(pg_db, F(key="other"), NOW)
    pg_db.commit()
    assert pg_db.scalar(select(func.count()).select_from(SystemAlert)) == 2


def test_auto_resolve_only_for_evaluated_kinds(pg_db):
    alerts.sync(pg_db, [F("k1", "a"), F("k2", "b")], {"k1", "k2"}, NOW)
    pg_db.commit()
    # k1 cleared; k2's check did not run (not in ok_kinds): it must stay open
    res = alerts.sync(pg_db, [], {"k1"}, NOW + timedelta(minutes=1))
    pg_db.commit()
    assert res["resolved"] == 1
    st = {a.kind: a.status for a in pg_db.scalars(select(SystemAlert)).all()}
    assert st == {"k1": "resolved", "k2": "open"}


def test_resolved_alert_reopens_as_new_row(pg_db):
    alerts.sync(pg_db, [F()], {"k"}, NOW)
    alerts.sync(pg_db, [], {"k"}, NOW)
    pg_db.commit()
    alerts.sync(pg_db, [F()], {"k"}, NOW)
    pg_db.commit()
    rows = pg_db.scalars(select(SystemAlert).order_by(SystemAlert.id)).all()
    assert [(r.status, r.count) for r in rows] == [("resolved", 1), ("open", 1)]


def test_ack_keeps_acked_through_repeats_and_resolves(pg_db):
    a = alerts.raise_alert(pg_db, F(), NOW)
    alerts.ack(pg_db, a.id, NOW)
    pg_db.commit()
    again = alerts.raise_alert(pg_db, F(), NOW + timedelta(minutes=1))
    pg_db.commit()
    assert again.status == "acked" and again.count == 2 and again.acked_at == NOW
    alerts.sync(pg_db, [], {"k"}, NOW)
    pg_db.commit()
    assert pg_db.get(SystemAlert, a.id).status == "resolved"


# --------------------------------------------------------------------------- watcher cycle
def _watcher(factory, enabled=True):
    return HealthWatcher(factory, now=lambda: NOW)


def test_cycle_flags_missing_heartbeat_and_is_idempotent_across_restarts(pg_db, factory, monkeypatch):
    monkeypatch.setattr(settings, "research_worker_enabled", True)
    monkeypatch.setattr(settings, "watcher_enabled", False)
    monkeypatch.setattr(settings, "strategist_worker_enabled", False)
    for _ in range(3):  # a "restart" = a brand new HealthWatcher each time
        out = _watcher(factory).run_cycle()
        assert out["status"] == "ok"
    pg_db.expire_all()
    hb = pg_db.scalars(select(SystemAlert).where(SystemAlert.kind == "worker_heartbeat_stale")).all()
    assert len(hb) == 1 and hb[0].key == "research_worker" and hb[0].count == 3
    # the watcher's own heartbeat was written
    assert pg_db.get(WorkerHeartbeat, "health_watcher").status == "ok"


def test_cycle_resolves_when_condition_clears(pg_db, factory, monkeypatch):
    monkeypatch.setattr(settings, "research_worker_enabled", True)
    monkeypatch.setattr(settings, "watcher_enabled", False)
    monkeypatch.setattr(settings, "strategist_worker_enabled", False)
    _watcher(factory).run_cycle()
    heartbeat.beat(pg_db, "research_worker", "ok", {}, started_at=NOW, now=NOW)
    pg_db.commit()
    _watcher(factory).run_cycle()
    pg_db.expire_all()
    a = pg_db.scalars(select(SystemAlert).where(SystemAlert.kind == "worker_heartbeat_stale")).one()
    assert a.status == "resolved" and a.resolved_at == NOW


def test_failing_check_becomes_warning_and_never_resolves_its_kinds(pg_db, factory, monkeypatch):
    monkeypatch.setattr(settings, "research_worker_enabled", True)
    monkeypatch.setattr(settings, "watcher_enabled", False)
    monkeypatch.setattr(settings, "strategist_worker_enabled", False)
    _watcher(factory).run_cycle()  # raises the heartbeat alert

    def boom(*a, **k):
        raise RuntimeError("db exploded")
    monkeypatch.setattr(collect, "heartbeats", boom)
    out = _watcher(factory).run_cycle()
    assert out["status"] == "ok"  # the loop survived
    pg_db.expire_all()
    err = pg_db.scalars(select(SystemAlert).where(SystemAlert.kind == "health_check_error")).one()
    assert err.key == "worker_heartbeats" and err.severity == "warning"
    hb = pg_db.scalars(select(SystemAlert).where(SystemAlert.kind == "worker_heartbeat_stale")).one()
    assert hb.status == "open"  # not resolved on doubt


def test_cycle_flags_order_failures_budget_reconcile_and_backlog(pg_db, factory, monkeypatch):
    monkeypatch.setattr(settings, "research_daily_budget_usd", 1.0)
    monkeypatch.setattr(settings, "ops_research_backlog_threshold", 2)
    for _ in range(3):
        pg_db.add(AuditLog(event_type="order_error", actor="system", payload={"error": "x"},
                           ts=NOW - timedelta(minutes=5)))
    pg_db.add(AuditLog(event_type="reconcile_order_missing", actor="reconcile", payload={},
                       ts=NOW - timedelta(minutes=5)))
    pg_db.add(LLMCall(role="research", prompt_name="p", prompt_version="v", requested_model="m",
                      status="ok", cost_usd=0.9, created_at=NOW - timedelta(hours=1)))
    for i in range(3):
        jobs.enqueue(pg_db, kind="INVESTIGATE", symbol="AAA", job_key=f"k{i}")
    pg_db.commit()
    _watcher(factory).run_cycle()
    pg_db.expire_all()
    kinds = {a.kind for a in pg_db.scalars(select(SystemAlert)).all()}
    assert {"order_failures", "llm_budget", "reconcile_mismatch", "research_backlog"} <= kinds


# --------------------------------------------------------------------------- API
def test_status_empty_database(client):
    body = client.get("/ops/status").json()
    assert body["trading_mode"] == "paper"
    assert body["automation"] == {"mode": "manual", "kill_switch": False}
    assert body["queue"]["research_jobs"] == {"queued": 0, "leased": 0, "done": 0, "failed": 0}
    assert body["alerts"] == {"open": 0, "critical": 0}
    assert body["recent_errors"] == []
    d = body["data_freshness"]
    assert d["latest_quote_age_sec"] is None and d["latest_bar_age_sec"] is None
    assert isinstance(d["market_open"], bool)
    assert set(body["llm"]["roles"]) == {"strategist", "position", "research", "watcher"}
    assert body["llm"]["spend_today_usd"] == 0
    for r in body["llm"]["roles"].values():
        assert {"model", "effort", "spend_today_usd", "daily_budget_usd"} == set(r)
    # enabled workers with no heartbeat are listed as stale; none enabled by default
    assert body["workers"] == []


def test_status_partial_data_and_missing_enabled_worker(client, pg_db, monkeypatch):
    monkeypatch.setattr(settings, "research_worker_enabled", True)
    now = datetime.now(timezone.utc)
    heartbeat.beat(pg_db, "market_watcher", "ok", {"note": "hi"}, started_at=now, now=now)
    pg_db.add(AuditLog(event_type="order_error", actor="system", payload={"error": "kaboom"}))
    pg_db.add(LLMCall(role="watcher", prompt_name="p", prompt_version="v", requested_model="m",
                      status="ok", cost_usd=0.25, created_at=now))
    alerts.raise_alert(pg_db, F(sev="critical"))
    pg_db.commit()
    body = client.get("/ops/status").json()
    by = {w["worker"]: w for w in body["workers"]}
    assert by["market_watcher"]["stale"] is False and by["market_watcher"]["details"] == {"note": "hi"}
    assert by["research_worker"] == {"worker": "research_worker", "status": "missing", "last_beat": None,
                                     "age_sec": None, "stale": True, "details": {}}
    assert body["llm"]["roles"]["watcher"]["spend_today_usd"] == 0.25
    assert body["alerts"] == {"open": 1, "critical": 1}
    assert [e["event"] for e in body["recent_errors"]] == ["order_error"]


def test_recent_errors_are_redacted_and_bounded(client, pg_db, monkeypatch):
    monkeypatch.setattr(settings, "alpaca_secret", "SUPERSECRETVALUE123")
    for _ in range(60):
        pg_db.add(AuditLog(event_type="order_error", actor="system",
                           payload={"error": "failed with SUPERSECRETVALUE123 " + "x" * 500}))
    pg_db.commit()
    resp = client.get("/ops/status")
    assert "SUPERSECRETVALUE123" not in resp.text
    errs = resp.json()["recent_errors"]
    assert len(errs) == 50 and all(len(e["detail"]) <= 300 for e in errs)
    assert "***" in errs[0]["detail"]


def test_alert_message_redacted_in_response(client, pg_db, monkeypatch):
    monkeypatch.setattr(settings, "alpaca_secret", "SUPERSECRETVALUE123")
    alerts.raise_alert(pg_db, F(msg="leak SUPERSECRETVALUE123"))
    pg_db.commit()
    assert "SUPERSECRETVALUE123" not in client.get("/ops/alerts").text


def test_alerts_list_filters_and_ack_is_audited(client, pg_db):
    a = alerts.raise_alert(pg_db, F("k1", "a", "critical"))
    alerts.raise_alert(pg_db, F("k2", "b"))
    alerts.sync(pg_db, [F("k1", "a", "critical")], {"k2"})
    pg_db.commit()
    assert client.get("/ops/alerts").json()[0]["kind"] == "k1"
    assert [x["kind"] for x in client.get("/ops/alerts?status=resolved").json()] == ["k2"]
    assert len(client.get("/ops/alerts?status=all").json()) == 2
    assert client.get("/ops/alerts?status=bogus").status_code == 422
    shape = client.get("/ops/alerts").json()[0]
    assert set(shape) == {"id", "kind", "key", "severity", "message", "details", "first_seen", "last_seen",
                          "count", "status", "acked_at"}
    r = client.post(f"/ops/alerts/{a.id}/ack")
    assert r.status_code == 200 and r.json()["status"] == "acked" and r.json()["acked_at"]
    assert client.get("/ops/alerts").json() == []
    assert client.get("/ops/alerts?status=acked").json()[0]["id"] == a.id
    ev = pg_db.scalars(select(AuditLog).where(AuditLog.event_type == "system_alert_acked")).all()
    assert len(ev) == 1 and ev[0].actor == "user:test" and ev[0].payload["alert_id"] == a.id
    client.post(f"/ops/alerts/{a.id}/ack")  # idempotent, no second audit row
    assert pg_db.scalar(select(func.count()).select_from(AuditLog)
                        .where(AuditLog.event_type == "system_alert_acked")) == 1
    assert client.post("/ops/alerts/99999/ack").status_code == 404


def test_ops_endpoints_require_token(pg_db):
    app = FastAPI()
    app.include_router(ops_router.router)
    app.dependency_overrides[get_db] = lambda: pg_db
    c = TestClient(app)
    assert c.get("/ops/status").status_code == 401
    assert c.get("/ops/alerts").status_code == 401
    assert c.post("/ops/alerts/1/ack").status_code == 401


# --------------------------------------------------------------------------- recovery
def test_job_leased_by_crashed_worker_is_released_after_lease_expiry(pg_db):
    jobs.enqueue(pg_db, kind="INVESTIGATE", symbol="AAA", job_key="crash")
    pg_db.commit()
    first = jobs.lease(pg_db, "worker-1", lease_seconds=60, now=NOW)
    pg_db.commit()
    assert [j.leased_by for j in first] == ["worker-1"]
    # worker-1 crashes: it never completes or fails the job. Before expiry nobody gets it.
    assert jobs.lease(pg_db, "worker-2", now=NOW + timedelta(seconds=30)) == []
    again = jobs.lease(pg_db, "worker-2", lease_seconds=60, now=NOW + timedelta(seconds=61))
    pg_db.commit()
    assert [(j.leased_by, j.attempts) for j in again] == [("worker-2", 2)]


# --------------------------------------------------------------------------- retention
def _bar(ts, tf="1Min", sym="AAA"):
    return MarketBar(symbol=sym, timeframe=tf, ts=ts, open=1, high=1, low=1, close=1, volume=1, feed="iex")


def test_retention_defaults_keep_everything(pg_db):
    pg_db.add(_bar(NOW - timedelta(days=900)))
    pg_db.commit()
    out = retention.run(pg_db, apply=True, now=NOW)
    assert out["deleted"] == {} and all(t["rows"] == 0 for t in out["tables"].values())
    assert pg_db.scalar(select(func.count()).select_from(MarketBar)) == 1


def test_retention_dry_run_then_apply(pg_db):
    old = NOW - timedelta(days=40)
    pg_db.add_all([_bar(old), _bar(old, tf="1Day"), _bar(NOW - timedelta(days=1))])
    pg_db.add(SystemAlert(kind="k", key="a", severity="info", message="m", details={}, status="resolved",
                          resolved_at=old))
    pg_db.add(SystemAlert(kind="k", key="b", severity="info", message="m", details={}, status="open"))
    pg_db.add(AuditLog(event_type="order_error", actor="system", payload={}, ts=old))
    pg_db.add(LLMCall(role="watcher", prompt_name="p", prompt_version="v", requested_model="m",
                      status="ok", created_at=old))
    pg_db.commit()
    days = {"market_bars": 30, "system_alerts": 30}
    dry = retention.run(pg_db, apply=False, now=NOW, days=days)
    assert dry["deleted"] == {} and dry["tables"]["market_bars"]["rows"] == 1
    assert pg_db.scalar(select(func.count()).select_from(MarketBar)) == 3
    done = retention.run(pg_db, apply=True, now=NOW, days=days)
    assert done["deleted"] == {"market_bars": 1, "system_alerts": 1}
    left = pg_db.scalars(select(MarketBar)).all()
    assert sorted(b.timeframe for b in left) == ["1Day", "1Min"]  # daily bars + recent kept
    assert [a.key for a in pg_db.scalars(select(SystemAlert)).all()] == ["b"]
    # protected tables untouched
    assert pg_db.scalar(select(func.count()).select_from(LLMCall)) == 1
    assert pg_db.scalar(select(func.count()).select_from(AuditLog)
                        .where(AuditLog.event_type == "order_error")) == 1


def test_retention_keeps_jobs_referenced_by_bundles_and_never_prunes_active_jobs(pg_db):
    old = NOW - timedelta(days=40)
    ids = {}
    for key, status in (("ref", "done"), ("free", "done"), ("queued", "queued")):
        jid = jobs.enqueue(pg_db, kind="INVESTIGATE", symbol="AAA", job_key=key)
        pg_db.get(ResearchJob, jid).status = status
        ids[key] = jid
    pg_db.commit()
    pg_db.execute(ResearchJob.__table__.update().values(updated_at=old))
    pg_db.add(ResearchBundle(job_id=ids["ref"], symbol="AAA", kind="INVESTIGATE", status="ready", as_of=old,
                             evidence={}, context={}))
    pg_db.commit()
    retention.run(pg_db, apply=True, now=NOW, days={"research_jobs": 30})
    assert sorted(j.job_key for j in pg_db.scalars(select(ResearchJob)).all()) == ["queued", "ref"]


def test_retention_does_not_prune_bars_at_or_after_a_referenced_snapshot(pg_db):
    from app.models import AnalysisRun, MarketSnapshot
    snap_at = NOW - timedelta(days=50)
    snap = MarketSnapshot(purpose="analysis", as_of=snap_at, feed="iex", symbols=["AAA"], data={},
                          content_hash="h", quality={})
    pg_db.add(snap)
    pg_db.flush()
    pg_db.add(AnalysisRun(model="m", market_snapshot_id=snap.id, raw_response={}, input_context={}))
    pg_db.add_all([_bar(snap_at - timedelta(days=1)), _bar(snap_at + timedelta(days=1))])
    pg_db.commit()
    retention.run(pg_db, apply=True, now=NOW, days={"market_bars": 30})
    assert [b.ts for b in pg_db.scalars(select(MarketBar)).all()] == [snap_at + timedelta(days=1)]
    assert pg_db.scalar(select(func.count()).select_from(MarketSnapshot)) == 1


def test_retention_stale_heartbeats_only_for_workers_not_expected(pg_db, monkeypatch):
    monkeypatch.setattr(settings, "research_worker_enabled", True)
    old = NOW - timedelta(days=40)
    for w in ("research_worker", "retired_worker"):
        heartbeat.beat(pg_db, w, "ok", {}, started_at=old, now=old)
    pg_db.commit()
    retention.run(pg_db, apply=True, now=NOW, days={"worker_heartbeats": 30})
    assert [h.worker for h in pg_db.scalars(select(WorkerHeartbeat)).all()] == ["research_worker"]
