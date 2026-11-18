"""System health watcher: runs the pure health checks on a loop and maintains `system_alerts`.

Run as its own process (needs the schema at revision 0009 or later):

    HEALTH_WATCHER_ENABLED=true python -m app.workers.health_watcher

Every HEALTH_WATCHER_INTERVAL_SECONDS it runs the checks (stale heartbeats, stale market data,
order failures, LLM budget, research queue, reconciliation), raises/dedups alerts, resolves the
ones that cleared, and writes its own heartbeat. It never calls Claude, the broker or any other
network service, and never touches orders. A failing check becomes a warning alert; it never
crashes the loop. Restarting it is idempotent (alerts dedup on (kind, key)).
"""
import logging
import signal
import threading
from datetime import datetime, timezone

from sqlalchemy import text

from app.config import settings
from app.logging_config import log_event, run_id_var, safe_error
from app.services.ops import alerts, collect
from app.services.watcher import heartbeat

logger = logging.getLogger("trading.health")

WORKER = "health_watcher"
LOCK_KEY = 7_120_029          # pg advisory lock id (distinct from the other workers')


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


class HealthWatcher:
    def __init__(self, session_factory=None, *, now=None):
        if session_factory is None:
            from app.database import SessionLocal
            session_factory = SessionLocal
        self.session_factory = session_factory
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.started_at = self._now()
        self.cycles = 0
        self.stop_event = threading.Event()

    def _beat(self, status: str, details: dict) -> None:
        db = self.session_factory()
        try:
            heartbeat.beat(db, WORKER, status, details, started_at=self.started_at, now=self._now())
            db.commit()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            log_event(logger, "health.heartbeat_failed", logging.WARNING, error=safe_error(e))
        finally:
            db.close()

    def run_cycle(self) -> dict:
        """One evaluation. Never raises."""
        now = self._now()
        token = run_id_var.set(f"health-{now.strftime('%Y%m%dT%H%M%S')}")
        summary: dict = {"status": "ok"}
        db = self.session_factory()
        try:
            findings, ok_kinds = collect.run_checks(db, now)
            result = alerts.sync(db, findings, ok_kinds, now)
            db.commit()
            summary.update(result, findings=len(findings))
            for f in findings:
                log_event(logger, "health.finding", logging.WARNING, kind=f.kind, key=f.key,
                          severity=f.severity)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            summary = {"status": "error", "error": safe_error(e)}
            log_event(logger, "health.cycle_failed", logging.ERROR, error=safe_error(e))
        finally:
            db.close()
            run_id_var.reset(token)
        self.cycles += 1
        summary["cycle"] = self.cycles
        self._beat(summary["status"], {"last_cycle": summary})
        return summary

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            self.run_cycle()
            self.stop_event.wait(max(10, settings.health_watcher_interval_seconds))


def main() -> int:
    from app import db_migrate
    from app.database import engine
    from app.logging_config import configure_logging

    configure_logging()
    if not settings.health_watcher_enabled:
        log_event(logger, "health.disabled", logging.WARNING,
                  note="set HEALTH_WATCHER_ENABLED=true to run the health watcher")
        return 0
    status = db_migrate.schema_status()
    if not status["up_to_date"]:
        log_event(logger, "health.schema_out_of_date", logging.ERROR, current=status["current"],
                  head=status["head"])
        return 1
    lock_conn = try_acquire_lock(engine)
    if lock_conn is None:
        log_event(logger, "health.already_running", logging.ERROR)
        return 1
    watcher = HealthWatcher()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: watcher.stop_event.set())
    log_event(logger, "health.start", interval=settings.health_watcher_interval_seconds)
    try:
        watcher.run_forever()
    finally:
        watcher._beat("stopped", {})
        release_lock(lock_conn)
        log_event(logger, "health.stop", cycles=watcher.cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
