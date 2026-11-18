"""Job definitions, handlers and the tick that runs what is due.

Jobs (all ET): `briefing` weekdays 08:30, `scanner` every SCANNER_INTERVAL_MIN during market
hours (only when SCANNER_ENABLED), `weekly_eval` Saturday 10:00. A handler may only write
reports, research-queue rows and its own run record: no settings, strategy or automation change,
no broker call. Each (job, period) is claimed through `store.claim`, so a period never runs twice.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.logging_config import log_event, safe_error
from app.models import PositionManagement, Watchlist
from app.services import briefing, scanner
from app.services.scheduler import schedule, store

logger = logging.getLogger("trading.scheduler")

BACKEND_DIR = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class JobDef:
    name: str
    schedule: schedule.Schedule
    handler: Callable[[Session, datetime, str], dict]
    enabled: Callable[[], bool] = lambda: True


# --------------------------------------------------------------------------- handlers
def run_briefing(db: Session, now: datetime, period: str) -> dict:
    return briefing.run(db, now)


def run_scanner(db: Session, now: datetime, period: str) -> dict:
    result = scanner.scan(db, now)
    return result


def report_dir() -> Path:
    p = Path(settings.backtest_report_dir)
    return p if p.is_absolute() else BACKEND_DIR / p


def _symbols(db: Session) -> list[str]:
    watch = db.scalars(select(Watchlist.ticker).where(Watchlist.active.is_(True))).all()
    held = db.scalars(select(PositionManagement.symbol).where(PositionManagement.is_open.is_(True))).all()
    return sorted({s.upper() for s in [*watch, *held]})


def run_weekly_eval(db: Session, now: datetime, period: str) -> dict:
    """Weekly review + walk-forward baseline + AI replay. Writes reports only; re-running a
    period overwrites the same deterministic files and replaces that week's review report."""
    from app.backtest import ai_run
    from app.backtest import run as baseline_run
    from app.services.review import daily

    saturday = date.fromisoformat(period)
    end = saturday - timedelta(days=1)  # the Friday that closed the week
    created_at = datetime.combine(saturday, time(10, 0), tzinfo=schedule.ET).isoformat()
    out_dir = report_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {"period": period, "components": {}}
    errors: list[str] = []

    try:
        reports = daily.run_review(db, end, weekly=True)
        result["components"]["review"] = {"status": "ok", "report_ids": [r.id for r in reports]}
    except Exception as e:  # noqa: BLE001
        db.rollback()
        errors.append(f"review: {safe_error(e)}")
        result["components"]["review"] = {"status": "error", "error": safe_error(e)}

    symbols = _symbols(db)
    jobs = {
        "baseline": (baseline_run.main, f"weekly-baseline-{period}.json", [
            "--symbols", ",".join(symbols), "--start", (end - timedelta(days=365)).isoformat(),
            "--end", end.isoformat(), "--split", "walk-forward"], bool(symbols)),
        "ai_replay": (ai_run.main, f"weekly-ai-replay-{period}.json", [
            "--start", (end - timedelta(days=90)).isoformat(), "--end", end.isoformat(),
            "--split", "none"], True),
    }
    for name, (fn, fname, argv, runnable) in jobs.items():
        if not runnable:
            result["components"][name] = {"status": "skipped", "reason": "no_symbols"}
            continue
        path = out_dir / fname
        try:
            code = fn([*argv, "--out", str(path), "--created-at", created_at], session=db)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            errors.append(f"{name}: {safe_error(e)}")
            result["components"][name] = {"status": "error", "error": safe_error(e)}
            continue
        result["components"][name] = ({"status": "ok", "file": fname} if code == 0
                                      else {"status": "no_data", "exit_code": code})
    if errors:
        raise RuntimeError("; ".join(errors))
    return result


def job_defs() -> list[JobDef]:
    return [
        JobDef("briefing", schedule.Schedule("weekday_at", at=time(8, 30), grace=timedelta(hours=6)),
               run_briefing),
        JobDef("scanner", schedule.Schedule("market_interval", interval_min=settings.scanner_interval_min),
               run_scanner, lambda: settings.scanner_enabled),
        JobDef("weekly_eval", schedule.Schedule("weekly_at", at=time(10, 0), weekday=5,
                                                grace=timedelta(hours=36)), run_weekly_eval),
    ]


# --------------------------------------------------------------------------- tick
def run_due(session_factory, now: datetime, defs: list[JobDef] | None = None) -> list[dict]:
    """Run every enabled job whose period is due at `now` and not yet done. Never raises."""
    out = []
    for job in defs if defs is not None else job_defs():
        try:
            if not job.enabled():
                continue
            period = schedule.due_period(job.schedule, now)
            if period is None:
                continue
            out.append(_run_one(session_factory, job, period, now))
        except Exception as e:  # noqa: BLE001 - one broken job must not stop the others
            log_event(logger, "scheduler.job_error", logging.ERROR, job=job.name, error=safe_error(e))
            out.append({"job": job.name, "status": "error", "error": safe_error(e)})
    return [o for o in out if o]


def _run_one(session_factory, job: JobDef, period: str, now: datetime) -> dict | None:
    claim_db = session_factory()
    try:
        store.abandon_stale(claim_db, now=now)
        run = store.claim(claim_db, job.name, period, now=now)
        claim_db.commit()
        if run is None:
            return None
        run_id, attempt = run.id, run.attempts
    except Exception:
        claim_db.rollback()
        raise
    finally:
        claim_db.close()

    db = session_factory()
    status, result, error = "succeeded", None, None
    try:
        result = job.handler(db, now, period)
        if result.get("skipped"):
            status = "skipped"
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        status, error = "failed", safe_error(e)
        log_event(logger, "scheduler.job_failed", logging.ERROR, job=job.name, period=period,
                  attempt=attempt, error=error)
    try:
        row = db.get(store.ScheduledJobRun, run_id)
        store.finish(db, row, status, result=result, error=error, now=now)
        db.commit()
    finally:
        db.close()
    log_event(logger, "scheduler.job_done", job=job.name, period=period, status=status)
    return {"job": job.name, "period": period, "status": status, "attempt": attempt}
