"""Read-only views of the recurring jobs: morning briefings and recent scheduler runs."""
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import DB, Actor
from app.models import ScheduledJobRun
from app.services.scheduler import store

router = APIRouter(tags=["briefings"])


def _briefings(db: Session, limit: int) -> list[ScheduledJobRun]:
    return list(db.scalars(
        select(ScheduledJobRun).where(ScheduledJobRun.job_name == "briefing",
                                      ScheduledJobRun.status == "succeeded")
        .order_by(ScheduledJobRun.period_key.desc()).limit(limit)).all())


@router.get("/briefings")
def list_briefings(limit: int = Query(10, ge=1, le=100), db: Session = DB, _actor: str = Actor):
    return [store.serialize(r) for r in _briefings(db, limit)]


@router.get("/briefings/latest")
def latest_briefing(db: Session = DB, _actor: str = Actor):
    rows = _briefings(db, 1)
    if not rows:
        raise HTTPException(404, "no briefing yet")
    return store.serialize(rows[0])


@router.get("/scheduler/runs")
def scheduler_runs(job: str | None = Query(None, max_length=64), limit: int = Query(50, ge=1, le=500),
                   db: Session = DB, _actor: str = Actor):
    return [store.serialize(r, with_result=False) for r in store.recent(db, job, limit)]
