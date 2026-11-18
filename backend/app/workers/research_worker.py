"""Research worker: turns watcher jobs into evidence bundles and specialist analyst reports.

Run as its own process (needs the schema at revision 0008 or later):

    RESEARCH_WORKER_ENABLED=true python -m app.workers.research_worker

Every RESEARCH_WORKER_INTERVAL_SECONDS it:
  1. expires bundles that were not strategized before their TTL (collecting/ready -> expired);
  2. checks the research-role daily/monthly budget; when exhausted it leases nothing;
  3. leases INVESTIGATE / REVIEW_POSITION jobs from `research_jobs` (bounded attempts);
  4. builds ONE frozen evidence bundle per job, runs the five analysts (each stores one
     `analyst_reports` row; a failing analyst never affects the others);
  5. marks the bundle `ready` when the technical report succeeded with a real view, else
     `failed`; completes the job, or fails it (retry while attempts remain);
  6. writes a heartbeat.
A job interrupted by the budget ceiling is released without consuming an attempt.

Read-only toward the broker (account and positions for context); never places, modifies or
cancels orders. Claude has no tools and its output is validated data. One instance at a time
(PostgreSQL advisory lock).
"""
import logging
import signal
import threading
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.config import settings
from app.logging_config import log_event, run_id_var, safe_error
from app.models import AnalystReportRow, ResearchBundle, ResearchJob
from app.services import llm
from app.services.research import analysts, bundle as bundle_mod
from app.services.research.contracts import ANALYSTS
from app.services.watcher import budget, heartbeat, jobs

logger = logging.getLogger("trading.research")

WORKER = "research_worker"
LOCK_KEY = 7_120_021          # pg advisory lock id (distinct from the watcher's)
KINDS = ("INVESTIGATE", "REVIEW_POSITION")
MAX_JOBS_PER_CYCLE = 5


def try_acquire_lock(engine):
    """Take the single-instance advisory lock on a dedicated connection kept for the process
    lifetime. Returns the connection, or None when another instance holds it."""
    conn = engine.connect()
    try:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar()
        conn.commit()
    except Exception:
        conn.close()
        raise
    if not got:
        conn.close()
        return None
    return conn


def release_lock(conn) -> None:
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
        conn.commit()
    finally:
        conn.close()


def expire_stale_bundles(db, now: datetime) -> int:
    """collecting/ready bundles past `expires_at` become `expired`. Caller commits."""
    rows = db.scalars(select(ResearchBundle).where(
        ResearchBundle.status.in_(("collecting", "ready")), ResearchBundle.expires_at < now)).all()
    for b in rows:
        b.status = "expired"
    db.flush()
    return len(rows)


def technical_succeeded(db, bundle_id: int) -> bool:
    row = db.scalar(select(AnalystReportRow).where(AnalystReportRow.bundle_id == bundle_id,
                                                   AnalystReportRow.analyst == "technical"))
    return bool(row and row.status == "ok" and (row.report or {}).get("stance") != "insufficient_data")


class ResearchWorker:
    def __init__(self, session_factory=None, *, now=None):
        if session_factory is None:
            from app.database import SessionLocal
            session_factory = SessionLocal
        self.session_factory = session_factory
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._cycle_lock = threading.Lock()
        self.started_at = self._now()
        self.cycles = 0
        self.stop_event = threading.Event()

    # ------------------------------------------------------------------ heartbeat
    def _beat(self, status: str, details: dict) -> None:
        db = self.session_factory()
        try:
            heartbeat.beat(db, WORKER, status, details, started_at=self.started_at, now=self._now())
            db.commit()
        except Exception as e:  # noqa: BLE001  a heartbeat failure must not kill the worker
            db.rollback()
            log_event(logger, "research.heartbeat_failed", logging.WARNING, error=safe_error(e))
        finally:
            db.close()

    # ------------------------------------------------------------------ job processing
    def process_job(self, db, job: ResearchJob, summary: dict) -> str:
        """Build the bundle, run the analysts, settle bundle and job. Returns
        ready | failed | budget_released | error. Commits as it goes."""
        job_id = job.id
        bundle_id = None
        try:
            bundle = bundle_mod.build_bundle(db, job, now=self._now())
            bundle_id = bundle.id
            db.commit()
            budget_hit = None
            for name in ANALYSTS:
                row = analysts.run_analyst(db, bundle, name, now=self._now())
                db.commit()
                if (row.error or "").startswith("budget_skipped"):
                    budget_hit = row.error
                    break
            if budget_hit:
                bundle.status, bundle.error = "failed", budget_hit
                job = db.get(ResearchJob, job_id)
                job.status, job.leased_by, job.lease_until = "queued", None, None
                job.attempts = max(0, job.attempts - 1)  # a budget pause is not a failed attempt
                db.commit()
                summary["budget_released"] += 1
                return "budget_released"
            if technical_succeeded(db, bundle.id):
                bundle.status, bundle.ready_at = "ready", self._now()
                jobs.complete(db, db.get(ResearchJob, job_id))
                db.commit()
                summary["ready"] += 1
                log_event(logger, "research.bundle_ready", bundle_id=bundle.id, symbol=bundle.symbol)
                return "ready"
            bundle.status, bundle.error = "failed", "technical analyst did not produce a usable report"
            outcome = jobs.fail(db, db.get(ResearchJob, job_id), bundle.error)
            db.commit()
            summary["failed"] += 1
            log_event(logger, "research.bundle_failed", logging.WARNING, bundle_id=bundle.id,
                      symbol=bundle.symbol, job_status=outcome)
            return "failed"
        except Exception as e:  # noqa: BLE001  fail the job (bounded retries), keep the worker alive
            db.rollback()
            err = safe_error(e, 500)
            log_event(logger, "research.job_error", logging.ERROR, job_id=job_id, error=err)
            if bundle_id is not None:
                b = db.get(ResearchBundle, bundle_id)
                if b is not None and b.status == "collecting":
                    b.status, b.error = "failed", err
            j = db.get(ResearchJob, job_id)
            if j is not None:
                jobs.fail(db, j, err)
            db.commit()
            summary["errors"] += 1
            return "error"

    # ------------------------------------------------------------------ cycle
    def run_cycle(self) -> dict:
        """One cycle. Never raises, never overlaps another cycle."""
        if not self._cycle_lock.acquire(blocking=False):
            return {"status": "skipped_overlap"}
        try:
            summary = self._cycle()
        except Exception as e:  # noqa: BLE001
            log_event(logger, "research.cycle_failed", logging.ERROR, error=safe_error(e))
            summary = {"status": "error", "error": safe_error(e)}
        finally:
            self._cycle_lock.release()
        self.cycles += 1
        summary["cycle"] = self.cycles
        summary["at"] = self._now().isoformat()
        self._beat(summary["status"], {"last_cycle": summary, "enabled": settings.research_worker_enabled})
        return summary

    def _cycle(self) -> dict:
        now = self._now()
        summary = {"status": "idle", "expired": 0, "jobs": 0, "ready": 0, "failed": 0, "errors": 0,
                   "budget_released": 0}
        db = self.session_factory()
        token = run_id_var.set(f"research-{now.strftime('%Y%m%dT%H%M%S')}")
        try:
            summary["expired"] = expire_stale_bundles(db, now)
            db.commit()
            for _ in range(MAX_JOBS_PER_CYCLE):
                if self.stop_event.is_set():
                    break
                ok, reason, st = budget.check(db, 0, role="research", now=self._now())
                if not ok:
                    summary.update(status="budget_paused", budget_skip_reason=reason, budget=st.as_dict())
                    log_event(logger, "research.budget_skip", logging.WARNING, reason=reason, **st.as_dict())
                    break
                leased = jobs.lease(db, WORKER, kinds=KINDS, limit=1, now=self._now())
                db.commit()  # persist the lease (and any terminally-failed expired leases)
                if not leased:
                    break
                summary["jobs"] += 1
                summary["status"] = "ok"
                outcome = self.process_job(db, leased[0], summary)
                if outcome == "budget_released":
                    summary["status"] = "budget_paused"
                    break
                if outcome in ("failed", "error"):
                    break  # the job is requeued: retry on a later cycle, not immediately
            return summary
        except Exception:
            db.rollback()
            raise
        finally:
            run_id_var.reset(token)
            db.close()

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            summary = self.run_cycle()
            busy = summary.get("status") == "ok" and summary.get("jobs", 0) >= MAX_JOBS_PER_CYCLE
            if not busy:
                self.stop_event.wait(max(5, settings.research_worker_interval_seconds))


def main() -> int:
    from app import db_migrate
    from app.database import engine
    from app.logging_config import configure_logging

    configure_logging()
    if not settings.research_worker_enabled:
        log_event(logger, "research.disabled", logging.WARNING,
                  note="set RESEARCH_WORKER_ENABLED=true to run the research worker")
        return 0
    status = db_migrate.schema_status()
    if not status["up_to_date"]:
        log_event(logger, "research.schema_out_of_date", logging.ERROR, current=status["current"],
                  head=status["head"])
        return 1
    lock_conn = try_acquire_lock(engine)
    if lock_conn is None:
        log_event(logger, "research.already_running", logging.ERROR)
        return 1
    worker = ResearchWorker()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: worker.stop_event.set())
    log_event(logger, "research.start", interval=settings.research_worker_interval_seconds,
              model=llm.get_role("research").model)
    try:
        worker.run_forever()
    finally:
        worker._beat("stopped", {"enabled": settings.research_worker_enabled})
        release_lock(lock_conn)
        log_event(logger, "research.stop", cycles=worker.cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
