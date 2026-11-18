"""DB-backed scheduler tests: idempotent claims (incl. a real two-thread race), stale/failed
recovery with the retry ceiling, the tick, the weekly evaluation and the API. No broker, Claude
or network access."""
import threading
from datetime import datetime, time, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import get_db
from app.deps import require_token
from app.models import ReviewReport, ScheduledJobRun, WorkerHeartbeat
from app.routers import briefings as briefings_router
from app.services.scheduler import runner, store
from app.services.scheduler.schedule import Schedule
from app.workers.scheduler_worker import SchedulerWorker

# Tuesday 2026-01-06 08:45 ET
NOW = datetime(2026, 1, 6, 13, 45, tzinfo=timezone.utc)


@pytest.fixture()
def factory(pg_db):
    return sessionmaker(bind=pg_db.get_bind(), future=True)


def runs(db, name=None):
    q = select(ScheduledJobRun).order_by(ScheduledJobRun.id)
    if name:
        q = q.where(ScheduledJobRun.job_name == name)
    db.expire_all()
    return list(db.scalars(q))


# --------------------------------------------------------------------------- claim store
def test_second_claim_of_same_period_is_refused(pg_db):
    a = store.claim(pg_db, "j", "p1", now=NOW)
    pg_db.commit()
    assert a is not None and a.status == "running" and a.attempts == 1
    assert store.claim(pg_db, "j", "p1", now=NOW) is None
    assert store.claim(pg_db, "j", "p2", now=NOW) is not None  # another period is independent


def test_succeeded_and_skipped_periods_never_rerun(pg_db):
    for status in ("succeeded", "skipped"):
        run = store.claim(pg_db, "j", status, now=NOW)
        store.finish(pg_db, run, status, result={"x": 1}, now=NOW)
        pg_db.commit()
        assert store.claim(pg_db, "j", status, now=NOW + timedelta(days=30)) is None


def test_two_workers_racing_one_period_only_one_claims(factory):
    results, barrier = [], threading.Barrier(2)

    def worker():
        db = factory()
        try:
            barrier.wait()
            run = store.claim(db, "race", "2026-01-06", now=NOW)
            db.commit()
            results.append(run is not None)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [False, True]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ScheduledJobRun)) == 1


def test_stale_running_is_recovered_up_to_the_retry_ceiling(pg_db):
    assert store.claim(pg_db, "j", "p", now=NOW, stale_sec=600, max_attempts=3) is not None
    pg_db.commit()
    # still fresh: not recoverable
    assert store.claim(pg_db, "j", "p", now=NOW + timedelta(seconds=300), stale_sec=600,
                       max_attempts=3) is None
    t = NOW
    for expected in (2, 3):  # worker "crashed": never finished
        t += timedelta(seconds=700)
        run = store.claim(pg_db, "j", "p", now=t, stale_sec=600, max_attempts=3)
        pg_db.commit()
        assert run is not None and run.attempts == expected
    t += timedelta(seconds=700)  # out of attempts
    assert store.claim(pg_db, "j", "p", now=t, stale_sec=600, max_attempts=3) is None
    assert store.abandon_stale(pg_db, now=t, stale_sec=600, max_attempts=3) == 1
    pg_db.commit()
    row = runs(pg_db)[0]
    assert row.status == "failed" and "abandoned" in row.error


def test_failed_run_retries_after_delay_until_ceiling(pg_db):
    t = NOW
    for attempt in (1, 2):
        run = store.claim(pg_db, "j", "p", now=t, retry_delay_sec=300, max_attempts=2)
        assert run is not None and run.attempts == attempt
        store.finish(pg_db, run, "failed", error="boom", now=t)
        pg_db.commit()
        assert store.claim(pg_db, "j", "p", now=t + timedelta(seconds=100), retry_delay_sec=300,
                           max_attempts=2) is None  # too soon
        t += timedelta(seconds=400)
    assert store.claim(pg_db, "j", "p", now=t, retry_delay_sec=300, max_attempts=2) is None  # ceiling


def test_finish_rejects_bad_status(pg_db):
    run = store.claim(pg_db, "j", "p", now=NOW)
    with pytest.raises(ValueError):
        store.finish(pg_db, run, "running")


# --------------------------------------------------------------------------- tick / runner
def _defs(calls, fail=False):
    def handler(db, now, period):
        calls.append(period)
        if fail:
            raise RuntimeError("boom api_key=sk-ant-secret1234567890")
        return {"ok": True}
    return [runner.JobDef("briefing", Schedule("weekday_at", at=time(8, 30)), handler)]


def test_tick_runs_due_period_once_across_restarts(factory):
    calls = []
    defs = _defs(calls)
    first = runner.run_due(factory, NOW, defs)
    again = runner.run_due(factory, NOW + timedelta(minutes=5), defs)  # a restarted worker
    assert calls == ["2026-01-06"]
    assert [r["status"] for r in first] == ["succeeded"] and again == []
    with factory() as db:
        rows = runs(db)
        assert len(rows) == 1 and rows[0].status == "succeeded" and rows[0].result == {"ok": True}


def test_failed_handler_is_recorded_redacted_and_retried_later(factory, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-secret1234567890")
    calls = []
    defs = _defs(calls, fail=True)
    out = runner.run_due(factory, NOW, defs)
    assert out[0]["status"] == "failed"
    with factory() as db:
        row = runs(db)[0]
        assert row.status == "failed" and "sk-ant-secret" not in row.error
    assert runner.run_due(factory, NOW + timedelta(seconds=10), defs) == []  # inside retry delay
    later = NOW + timedelta(seconds=settings.scheduler_retry_delay_sec + 5)
    assert runner.run_due(factory, later, defs)[0]["attempt"] == 2
    assert len(calls) == 2


def test_disabled_job_never_runs(factory):
    calls = []
    d = _defs(calls)[0]
    off = runner.JobDef(d.name, d.schedule, d.handler, lambda: False)
    assert runner.run_due(factory, NOW, [off]) == []
    assert calls == []


def test_skipped_result_is_recorded_as_skipped(factory):
    d = runner.JobDef("briefing", _defs([])[0].schedule, lambda db, n, p: {"skipped": "nothing"})
    assert runner.run_due(factory, NOW, [d])[0]["status"] == "skipped"


def test_worker_tick_writes_heartbeat_and_survives_job_errors(factory):
    def explode(db, now, period):
        raise RuntimeError("x")
    d = runner.JobDef("briefing", _defs([])[0].schedule, explode)
    w = SchedulerWorker(factory, now=lambda: NOW, defs=[d])
    summary = w.run_tick()
    assert summary["status"] == "ok" and summary["ran"][0]["status"] == "failed"
    with factory() as db:
        hb = db.get(WorkerHeartbeat, "scheduler")
        assert hb is not None and hb.status == "ok"


def test_default_job_defs_gate_scanner(monkeypatch):
    monkeypatch.setattr(settings, "scanner_enabled", False)
    enabled = {j.name: j.enabled() for j in runner.job_defs()}
    assert enabled == {"briefing": True, "scanner": False, "weekly_eval": True}
    monkeypatch.setattr(settings, "scanner_enabled", True)
    assert {j.name: j.enabled() for j in runner.job_defs()}["scanner"] is True


# --------------------------------------------------------------------------- weekly evaluation
SAT = datetime(2026, 1, 10, 15, 5, tzinfo=timezone.utc)  # Saturday 10:05 ET


def test_weekly_eval_writes_review_and_deterministic_reports(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backtest_report_dir", str(tmp_path))
    monkeypatch.setattr(settings, "review_llm_summary", False)
    seen = {}

    def fake_baseline(argv, session=None):
        seen["baseline"] = argv
        open(argv[argv.index("--out") + 1], "w").write("{}")
        return 0

    def fake_replay(argv, session=None):
        seen["replay"] = argv
        return 2  # no recorded proposals

    from app.backtest import ai_run
    from app.backtest import run as baseline_run
    monkeypatch.setattr(baseline_run, "main", fake_baseline)
    monkeypatch.setattr(ai_run, "main", fake_replay)
    with factory() as db:
        from app.models import Watchlist
        db.add(Watchlist(ticker="AAPL"))
        db.commit()
    defs = [j for j in runner.job_defs() if j.name == "weekly_eval"]
    out = runner.run_due(factory, SAT, defs)
    assert out[0]["status"] == "succeeded"
    assert (tmp_path / "weekly-baseline-2026-01-10.json").exists()
    assert "walk-forward" in seen["baseline"] and "AAPL" in seen["baseline"]
    assert seen["baseline"][seen["baseline"].index("--end") + 1] == "2026-01-09"
    assert seen["replay"][seen["replay"].index("--created-at") + 1].startswith("2026-01-10T10:00:00-05:00")
    with factory() as db:
        res = runs(db, "weekly_eval")[0].result["components"]
        assert res["review"]["status"] == "ok"
        assert res["baseline"]["file"] == "weekly-baseline-2026-01-10.json"
        assert res["ai_replay"]["status"] == "no_data"
        assert db.scalar(select(func.count()).select_from(ReviewReport).where(
            ReviewReport.kind == "weekly")) == 1
    assert runner.run_due(factory, SAT + timedelta(hours=1), defs) == []  # never twice


def test_weekly_eval_component_error_fails_the_run_for_retry(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backtest_report_dir", str(tmp_path))
    from app.backtest import ai_run

    def bad(argv, session=None):
        raise RuntimeError("replay exploded")

    monkeypatch.setattr(ai_run, "main", bad)
    defs = [j for j in runner.job_defs() if j.name == "weekly_eval"]
    out = runner.run_due(factory, SAT, defs)  # no symbols: baseline skipped; review still ran
    assert out[0]["status"] == "failed"
    with factory() as db:
        assert "replay exploded" in runs(db, "weekly_eval")[0].error


def test_weekly_eval_does_not_touch_settings_or_automation(factory, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backtest_report_dir", str(tmp_path))
    before = (settings.trading_mode, settings.scanner_enabled, settings.scheduler_enabled)
    runner.run_due(factory, SAT, [j for j in runner.job_defs() if j.name == "weekly_eval"])
    assert before == (settings.trading_mode, settings.scanner_enabled, settings.scheduler_enabled)


# --------------------------------------------------------------------------- API
@pytest.fixture()
def client(pg_db):
    app = FastAPI()
    app.include_router(briefings_router.router)
    app.dependency_overrides[get_db] = lambda: pg_db
    app.dependency_overrides[require_token] = lambda: "user:test"
    return TestClient(app)


def test_runs_api_filters_and_limits(pg_db, client):
    for i, name in enumerate(["a", "b", "a"]):
        r = store.claim(pg_db, name, f"p{i}", now=NOW + timedelta(minutes=i))
        store.finish(pg_db, r, "succeeded", result={"big": "x"}, now=NOW)
    pg_db.commit()
    rows = client.get("/scheduler/runs").json()
    assert len(rows) == 3 and "result" not in rows[0]
    assert [r["period_key"] for r in client.get("/scheduler/runs?job=a").json()] == ["p2", "p0"]
    assert len(client.get("/scheduler/runs?limit=1").json()) == 1
    assert client.get("/scheduler/runs?limit=0").status_code == 422


def test_briefings_api(pg_db, client):
    assert client.get("/briefings/latest").status_code == 404
    assert client.get("/briefings").json() == []
    for day in ("2026-01-05", "2026-01-06"):
        r = store.claim(pg_db, "briefing", day, now=NOW)
        store.finish(pg_db, r, "succeeded", result={"date": day}, now=NOW)
    r = store.claim(pg_db, "briefing", "2026-01-07", now=NOW)
    store.finish(pg_db, r, "failed", error="x", now=NOW)
    pg_db.commit()
    assert client.get("/briefings/latest").json()["result"] == {"date": "2026-01-06"}
    assert [b["period_key"] for b in client.get("/briefings").json()] == ["2026-01-06", "2026-01-05"]
