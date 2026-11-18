"""Retention: prune old operational data.

    python -m app.services.ops.retention            # dry run: report what would go
    python -m app.services.ops.retention --apply    # delete

Per-table day limits come from settings (RETENTION_*_DAYS); 0 means keep forever (default).
Never touches audit_log, llm_calls, orders, trade_intents, journal/outcome tables or
snapshots. Market bars and news are never pruned at or after the oldest snapshot that an
analysis run, intent or research bundle references, so replay inputs stay intact. Research
jobs referenced by a bundle are kept. No broker, Claude or network access.
"""
import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.logging_config import log_event
from app.models import (
    AnalysisRun, MarketBar, MarketSnapshot, NewsEvent, ResearchBundle, ResearchJob, SystemAlert,
    TradeIntent, WorkerHeartbeat,
)
from app.services.ops import collect

logger = logging.getLogger("trading.retention")

# Tables this module is allowed to delete from. Anything else is never touched.
PRUNABLE = ("system_alerts", "worker_heartbeats", "market_bars", "news_events", "research_jobs")


def _cutoff(now: datetime, days: int) -> datetime | None:
    return now - timedelta(days=days) if days and days > 0 else None


def replay_floor(db: Session) -> datetime | None:
    """Oldest as_of of any snapshot referenced by a run, intent or bundle (None if none)."""
    refs = set()
    for col in (AnalysisRun.market_snapshot_id, TradeIntent.market_snapshot_id,
                ResearchBundle.market_snapshot_id):
        refs.update(r for (r,) in db.execute(select(col).where(col.is_not(None)).distinct()).all())
    if not refs:
        return None
    return db.scalar(select(func.min(MarketSnapshot.as_of)).where(MarketSnapshot.id.in_(refs)))


def _limit(cutoff: datetime | None, floor: datetime | None) -> datetime | None:
    if cutoff is None:
        return None
    return min(cutoff, floor) if floor is not None else cutoff


def plan(db: Session, now: datetime, days: dict[str, int] | None = None) -> dict[str, dict]:
    """{table: {days, cutoff, rows}} — the number of rows each prune would delete."""
    days = days or {
        "system_alerts": settings.retention_system_alerts_days,
        "worker_heartbeats": settings.retention_worker_heartbeats_days,
        "market_bars": settings.retention_market_bars_days,
        "news_events": settings.retention_news_events_days,
        "research_jobs": settings.retention_research_jobs_days,
    }
    floor = replay_floor(db)
    expected = collect.expected_workers()
    out: dict[str, dict] = {}
    for table, n in days.items():
        c = _cutoff(now, n)
        out[table] = {"days": n, "cutoff": c.isoformat() if c else None, "rows": 0, "_stmt": None}
        if c is None:
            continue
        if table == "system_alerts":
            where = (SystemAlert.status == "resolved", SystemAlert.resolved_at < c)
            model = SystemAlert
        elif table == "worker_heartbeats":
            where = (WorkerHeartbeat.last_beat < c, WorkerHeartbeat.worker.not_in(expected or [""]))
            model = WorkerHeartbeat
        elif table == "market_bars":
            c = _limit(c, floor)
            where = (MarketBar.timeframe == "1Min", MarketBar.ts < c)
            model = MarketBar
        elif table == "news_events":
            c = _limit(c, floor)
            where = (NewsEvent.published_at < c,)
            model = NewsEvent
        elif table == "research_jobs":
            where = (ResearchJob.status.in_(("done", "failed")), ResearchJob.updated_at < c,
                     ~select(ResearchBundle.id).where(ResearchBundle.job_id == ResearchJob.id).exists())
            model = ResearchJob
        else:  # pragma: no cover - guarded by PRUNABLE
            continue
        out[table]["cutoff"] = c.isoformat()
        out[table]["rows"] = int(db.scalar(select(func.count()).select_from(model).where(*where)) or 0)
        out[table]["_stmt"] = delete(model).where(*where)
    return out


def run(db: Session, *, apply: bool = False, now: datetime | None = None,
        days: dict[str, int] | None = None) -> dict:
    """Plan (and with apply=True, execute) the prune. Commits when applying."""
    now = now or datetime.now(timezone.utc)
    p = plan(db, now, days)
    deleted: dict[str, int] = {}
    if apply:
        for table, info in p.items():
            if info["_stmt"] is not None and info["rows"]:
                deleted[table] = db.execute(info["_stmt"]).rowcount
        if deleted:
            from app.deps import audit
            audit(db, "retention_applied", "system", payload={"deleted": deleted})
        db.commit()
    report = {t: {k: v for k, v in info.items() if k != "_stmt"} for t, info in p.items()}
    return {"applied": apply, "tables": report, "deleted": deleted}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prune old operational data (dry run unless --apply).")
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    args = parser.parse_args(argv)
    from app.database import SessionLocal
    from app.logging_config import configure_logging

    configure_logging()
    db = SessionLocal()
    try:
        result = run(db, apply=args.apply)
    finally:
        db.close()
    log_event(logger, "retention.done", applied=result["applied"], deleted=result["deleted"])
    for table, info in result["tables"].items():
        print(f"{table}: keep {info['days'] or 'forever'} days, "
              f"{'deleted' if args.apply else 'would delete'} "
              f"{result['deleted'].get(table, 0) if args.apply else info['rows']} rows")
    if not args.apply:
        print("dry run: nothing was deleted (use --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
