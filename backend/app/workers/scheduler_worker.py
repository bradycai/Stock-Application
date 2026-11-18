"""Scheduler worker: runs the recurring jobs (morning briefing, candidate scanner, weekly
evaluation) on their ET schedules. Needs the schema at revision 0010 or later.

    SCHEDULER_ENABLED=true python -m app.workers.scheduler_worker

Every SCHEDULER_TICK_SECONDS it runs whatever period is due and not yet done (idempotent per
(job, period) through `scheduled_job_runs`), and writes a heartbeat under worker name
`scheduler`. The scanner additionally needs SCANNER_ENABLED=true. It never places orders and
never changes settings, strategies or automation mode.
"""
import logging
import signal
import threading
from datetime import datetime, timezone

from sqlalchemy import text

from app.config import settings
from app.logging_config import log_event, run_id_var, safe_error
from app.services.scheduler import runner
from app.services.watcher import heartbeat

logger = logging.getLogger("trading.scheduler")

WORKER = "scheduler"
LOCK_KEY = 7_120_037          # pg advisory lock id (distinct from the other workers')


def try_acquire_lock(engine):
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


class SchedulerWorker:
    def __init__(self, session_factory=None, *, now=None, defs=None):
        if session_factory is None:
            from app.database import SessionLocal
            session_factory = SessionLocal
        self.session_factory = session_factory
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.defs = defs
        self.started_at = self._now()
        self.ticks = 0
        self.stop_event = threading.Event()

    def _beat(self, status: str, details: dict) -> None:
        db = self.session_factory()
        try:
            heartbeat.beat(db, WORKER, status, details, started_at=self.started_at, now=self._now())
            db.commit()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            log_event(logger, "scheduler.heartbeat_failed", logging.WARNING, error=safe_error(e))
        finally:
            db.close()

    def run_tick(self) -> dict:
        """One pass over the due jobs. Never raises."""
        now = self._now()
        token = run_id_var.set(f"scheduler-{now.strftime('%Y%m%dT%H%M%S')}")
        try:
            ran = runner.run_due(self.session_factory, now, self.defs)
            summary = {"status": "ok", "ran": ran}
        except Exception as e:  # noqa: BLE001
            summary = {"status": "error", "error": safe_error(e)}
            log_event(logger, "scheduler.tick_failed", logging.ERROR, error=safe_error(e))
        finally:
            run_id_var.reset(token)
        self.ticks += 1
        summary["tick"] = self.ticks
        self._beat(summary["status"], {"last_tick": summary})
        return summary

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            self.run_tick()
            self.stop_event.wait(max(5, settings.scheduler_tick_seconds))


def main() -> int:
    from app import db_migrate
    from app.database import engine
    from app.logging_config import configure_logging

    configure_logging()
    if not settings.scheduler_enabled:
        log_event(logger, "scheduler.disabled", logging.WARNING,
                  note="set SCHEDULER_ENABLED=true to run the scheduler")
        return 0
    status = db_migrate.schema_status()
    if not status["up_to_date"]:
        log_event(logger, "scheduler.schema_out_of_date", logging.ERROR, current=status["current"],
                  head=status["head"])
        return 1
    lock_conn = try_acquire_lock(engine)
    if lock_conn is None:
        log_event(logger, "scheduler.already_running", logging.ERROR)
        return 1
    worker = SchedulerWorker()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: worker.stop_event.set())
    log_event(logger, "scheduler.start", tick=settings.scheduler_tick_seconds,
              scanner=settings.scanner_enabled, briefing_llm=settings.briefing_llm_summary)
    try:
        worker.run_forever()
    finally:
        worker._beat("stopped", {})
        release_lock(lock_conn)
        log_event(logger, "scheduler.stop", ticks=worker.ticks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
