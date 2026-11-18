"""Idempotent run store (table `scheduled_job_runs`).

`claim` is an insert-or-skip on the unique (job_name, period_key): of any number of workers
racing for one period exactly one gets the claim. A period is re-claimed only when its run is a
crashed `running` one (older than the stale timeout) or a `failed` one past the retry delay, and
only while attempts < max_attempts. `succeeded` and `skipped` periods are never re-run.
Callers commit.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.logging_config import safe_error
from app.models import ScheduledJobRun

STATUSES = ("running", "succeeded", "failed", "skipped")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def claim(db: Session, job_name: str, period_key: str, *, now: datetime | None = None,
          stale_sec: int | None = None, retry_delay_sec: int | None = None,
          max_attempts: int | None = None) -> ScheduledJobRun | None:
    """Claim (job, period) for this worker, or None when it is done, in flight, or out of attempts."""
    now = now or _now()
    stale = timedelta(seconds=stale_sec if stale_sec is not None else settings.scheduler_stale_run_sec)
    delay = timedelta(seconds=(retry_delay_sec if retry_delay_sec is not None
                               else settings.scheduler_retry_delay_sec))
    ceiling = max_attempts if max_attempts is not None else settings.scheduler_max_attempts
    row_id = db.execute(
        pg_insert(ScheduledJobRun).values(job_name=job_name, period_key=period_key, status="running",
                                          attempts=1, started_at=now)
        .on_conflict_do_nothing(constraint="uq_scheduled_job_runs_period")
        .returning(ScheduledJobRun.id)).scalar_one_or_none()
    if row_id is None:
        # Single atomic UPDATE: two workers cannot both win the re-claim.
        row_id = db.execute(
            update(ScheduledJobRun)
            .where(ScheduledJobRun.job_name == job_name, ScheduledJobRun.period_key == period_key,
                   ScheduledJobRun.attempts < ceiling,
                   or_(and_(ScheduledJobRun.status == "running", ScheduledJobRun.started_at < now - stale),
                       and_(ScheduledJobRun.status == "failed", ScheduledJobRun.finished_at < now - delay)))
            .values(status="running", attempts=ScheduledJobRun.attempts + 1, started_at=now,
                    finished_at=None, error=None)
            .returning(ScheduledJobRun.id)).scalar_one_or_none()
    if row_id is None:
        return None
    db.flush()
    return db.get(ScheduledJobRun, row_id, populate_existing=True)


def abandon_stale(db: Session, *, now: datetime | None = None, stale_sec: int | None = None,
                  max_attempts: int | None = None) -> int:
    """Mark crashed `running` runs that are out of attempts as failed, so they are visible."""
    now = now or _now()
    stale = timedelta(seconds=stale_sec if stale_sec is not None else settings.scheduler_stale_run_sec)
    ceiling = max_attempts if max_attempts is not None else settings.scheduler_max_attempts
    res = db.execute(update(ScheduledJobRun)
                     .where(ScheduledJobRun.status == "running", ScheduledJobRun.attempts >= ceiling,
                            ScheduledJobRun.started_at < now - stale)
                     .values(status="failed", finished_at=now, error="abandoned: worker crashed"))
    return res.rowcount or 0


def finish(db: Session, run: ScheduledJobRun, status: str, *, result: dict | None = None,
           error: object = None, now: datetime | None = None) -> None:
    if status not in STATUSES or status == "running":
        raise ValueError(f"bad final status {status!r}")
    run.status = status
    run.finished_at = now or _now()
    run.result = result
    run.error = safe_error(error, 1000) if error is not None else None
    db.flush()


def recent(db: Session, job: str | None = None, limit: int = 50) -> list[ScheduledJobRun]:
    q = select(ScheduledJobRun).order_by(ScheduledJobRun.started_at.desc(), ScheduledJobRun.id.desc())
    if job:
        q = q.where(ScheduledJobRun.job_name == job)
    return list(db.scalars(q.limit(limit)).all())


def serialize(r: ScheduledJobRun, *, with_result: bool = True) -> dict:
    return {"id": r.id, "job_name": r.job_name, "period_key": r.period_key, "status": r.status,
            "attempts": r.attempts, "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "error": r.error, **({"result": r.result} if with_result else {})}
