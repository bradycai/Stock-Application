"""Opus strategist worker.

Run as its own process (needs the Phase 5 schema):

    STRATEGIST_WORKER_ENABLED=true python -m app.workers.strategist_worker

Each cycle it expires old bundles/proposals, leases `ready` research bundles (lease_until /
leased_by), and for each one asks Opus for a proposal, validates it, stores it in
`strategy_proposals`, and converts an accepted BUY/SELL into a PENDING trade intent that waits
in the normal approval flow. The bundle ends `strategized` (or `failed` with an error). It
skips Claude calls when the daily/monthly strategist budget would be exceeded. One instance
at a time (PostgreSQL advisory lock). It only READS the broker (account, positions) and never
places, modifies or cancels orders.
"""
import logging
import signal
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, text

from app.config import settings
from app.logging_config import log_event, safe_error
from app.models import ResearchBundle, StrategyProposal
from app.services import llm
from app.services.strategist import pipeline
from app.services.watcher import heartbeat

logger = logging.getLogger("trading.strategist")

WORKER = "strategist_worker"
LOCK_KEY = 7_120_022          # pg advisory lock id (unique to this worker)
BATCH = 3                     # bundles leased per cycle


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


def expire_old(db, now: datetime) -> dict:
    """Mark ready bundles past expires_at `expired`, and un-finished proposals `expired`."""
    bundles = db.scalars(select(ResearchBundle).where(
        ResearchBundle.status == "ready", ResearchBundle.expires_at.is_not(None),
        ResearchBundle.expires_at <= now)).all()
    for b in bundles:
        b.status = "expired"
        b.lease_until = None
        b.leased_by = None
    props = db.scalars(select(StrategyProposal).where(
        StrategyProposal.status == "proposed", StrategyProposal.expires_at.is_not(None),
        StrategyProposal.expires_at <= now)).all()
    for p in props:
        p.status = "expired"
    return {"bundles_expired": len(bundles), "proposals_expired": len(props)}


def lease_bundles(db, worker_id: str, now: datetime, limit: int = BATCH,
                  lease_seconds: int | None = None) -> list[ResearchBundle]:
    """Lease unleased (or lease-expired), unexpired `ready` bundles, oldest first."""
    rows = db.scalars(
        select(ResearchBundle).where(
            ResearchBundle.status == "ready",
            or_(ResearchBundle.lease_until.is_(None), ResearchBundle.lease_until <= now),
            or_(ResearchBundle.expires_at.is_(None), ResearchBundle.expires_at > now))
        .order_by(ResearchBundle.ready_at.asc().nulls_last(), ResearchBundle.id)
        .limit(limit).with_for_update(skip_locked=True)).all()
    until = now + timedelta(seconds=lease_seconds or settings.strategist_lease_seconds)
    for b in rows:
        b.lease_until = until
        b.leased_by = worker_id
    db.commit()
    return list(rows)


class StrategistWorker:
    def __init__(self, session_factory=None, *, call_fn=None, get_account=None, get_positions=None,
                 now=None, worker_id: str = WORKER):
        if session_factory is None:
            from app.database import SessionLocal
            session_factory = SessionLocal
        self.session_factory = session_factory
        self._call_fn = call_fn
        self._get_account = get_account
        self._get_positions = get_positions
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.worker_id = worker_id
        self._cycle_lock = threading.Lock()
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
            log_event(logger, "strategist.heartbeat_failed", logging.WARNING, error=safe_error(e))
        finally:
            db.close()

    def run_cycle(self) -> dict:
        if not self._cycle_lock.acquire(blocking=False):
            return {"status": "skipped_overlap"}
        try:
            summary = self._cycle()
        except Exception as e:  # noqa: BLE001
            log_event(logger, "strategist.cycle_failed", logging.ERROR, error=safe_error(e))
            summary = {"status": "error", "error": safe_error(e)}
        finally:
            self._cycle_lock.release()
        self.cycles += 1
        summary["cycle"] = self.cycles
        summary["at"] = self._now().isoformat()
        self._beat(summary["status"], {"last_cycle": summary,
                                       "enabled": settings.strategist_worker_enabled})
        return summary

    def _cycle(self) -> dict:
        now = self._now()
        summary: dict = {"status": "ok", "processed": 0, "outcomes": {}}
        db = self.session_factory()
        try:
            summary.update(expire_old(db, now))
            db.commit()
            for bundle in lease_bundles(db, self.worker_id, now):
                if self.stop_event.is_set():
                    self._release(db, bundle)
                    continue
                try:
                    res = pipeline.process_bundle(
                        db, bundle, call_fn=self._call_fn, get_account=self._get_account,
                        get_positions=self._get_positions, now=now)
                except pipeline.BudgetExceeded as e:
                    db.rollback()
                    self._release(db, bundle)
                    summary["status"] = "budget"
                    summary["budget_reason"] = e.reason
                    log_event(logger, "strategist.budget_skip", logging.WARNING, reason=e.reason)
                    continue
                except Exception as e:  # noqa: BLE001
                    db.rollback()
                    b = db.get(ResearchBundle, bundle.id)
                    pipeline._finish_bundle(b, "failed", safe_error(e))
                    res = {"bundle_id": bundle.id, "outcome": "failed", "error": safe_error(e)}
                    log_event(logger, "strategist.bundle_error", logging.ERROR, bundle_id=bundle.id,
                              error=safe_error(e))
                db.commit()
                summary["processed"] += 1
                summary["outcomes"][res["outcome"]] = summary["outcomes"].get(res["outcome"], 0) + 1
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return summary

    @staticmethod
    def _release(db, bundle) -> None:
        b = db.get(ResearchBundle, bundle.id)
        if b is not None and b.status == "ready":
            b.lease_until = None
            b.leased_by = None
        db.commit()

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            self.run_cycle()
            self.stop_event.wait(settings.strategist_poll_seconds)


def main() -> int:
    from app import db_migrate
    from app.database import engine
    from app.logging_config import configure_logging

    configure_logging()
    if not settings.strategist_worker_enabled:
        log_event(logger, "strategist.disabled", logging.WARNING,
                  note="set STRATEGIST_WORKER_ENABLED=true to run the strategist worker")
        return 0
    status = db_migrate.schema_status()
    if not status["up_to_date"]:
        log_event(logger, "strategist.schema_out_of_date", logging.ERROR, current=status["current"],
                  head=status["head"])
        return 1
    lock_conn = try_acquire_lock(engine)
    if lock_conn is None:
        log_event(logger, "strategist.already_running", logging.ERROR)
        return 1
    worker = StrategistWorker()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: worker.stop_event.set())
    log_event(logger, "strategist.start", model=llm.get_role("strategist").model)
    try:
        worker.run_forever()
    finally:
        worker._beat("stopped", {"enabled": settings.strategist_worker_enabled})
        release_lock(lock_conn)
        log_event(logger, "strategist.stop", cycles=worker.cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
