"""Read-only views of the market watcher: heartbeat, budget, decisions and the job queue."""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import DB, Actor
from app.models import ResearchJob, WatcherDecision
from app.services.watcher import budget, heartbeat, jobs

router = APIRouter(prefix="/watcher", tags=["watcher"])
WORKER = "market_watcher"


def _dt(t):
    return t.isoformat() if t else None


@router.get("/status")
def watcher_status(db: Session = DB, _actor: str = Actor):
    hb = heartbeat.get(db, WORKER)
    now = datetime.now(timezone.utc)
    beat = None
    if hb:
        age = (now - hb.last_beat).total_seconds()
        beat = {"status": hb.status, "started_at": _dt(hb.started_at), "last_beat": _dt(hb.last_beat),
                "age_seconds": round(age, 1),
                "stale": age > 3 * max(settings.watcher_interval_seconds,
                                       settings.watcher_closed_sleep_seconds),
                "details": hb.details}
    return {
        "enabled": settings.watcher_enabled,
        "model": settings.watcher_model,
        "interval_seconds": settings.watcher_interval_seconds,
        "heartbeat": beat,
        "last_cycle": (hb.details or {}).get("last_cycle") if hb else None,
        "budget": budget.status(db).as_dict(),
        "queue": jobs.counts(db),
    }


@router.get("/decisions")
def watcher_decisions(limit: int = Query(50, ge=1, le=500), db: Session = DB, _actor: str = Actor):
    rows = db.scalars(select(WatcherDecision).order_by(WatcherDecision.id.desc()).limit(limit)).all()
    return [{
        "id": d.id, "created_at": _dt(d.created_at), "symbol": d.symbol, "action": d.action,
        "event_type": d.event_type, "urgency": d.urgency, "evidence_ids": d.evidence_ids,
        "explanation": d.explanation, "expires_at": _dt(d.expires_at), "prompt_version": d.prompt_version,
        "model": d.model, "run_id": d.run_id, "signal_event_ids": d.signal_event_ids,
        "downgraded": d.downgraded,
    } for d in rows]


@router.get("/jobs")
def watcher_jobs(status: str | None = Query(None), limit: int = Query(100, ge=1, le=500),
                 db: Session = DB, _actor: str = Actor):
    if status is not None and status not in jobs.STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {list(jobs.STATUSES)}")
    q = select(ResearchJob).order_by(ResearchJob.id.desc()).limit(limit)
    if status:
        q = q.where(ResearchJob.status == status)
    return [{
        "id": j.id, "job_key": j.job_key, "kind": j.kind, "symbol": j.symbol, "status": j.status,
        "attempts": j.attempts, "max_attempts": j.max_attempts, "lease_until": _dt(j.lease_until),
        "leased_by": j.leased_by, "decision_id": j.decision_id, "signal_event_ids": j.signal_event_ids,
        "payload": j.payload, "last_error": j.last_error, "created_at": _dt(j.created_at),
        "updated_at": _dt(j.updated_at),
    } for j in db.scalars(q).all()]
