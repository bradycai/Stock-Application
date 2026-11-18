"""Research job queue (table `research_jobs`).

- enqueue: idempotent on `job_key` (INSERT ... ON CONFLICT DO NOTHING).
- lease: FOR UPDATE SKIP LOCKED, so concurrent consumers never get the same job; a lease
  that expired makes the job available again. Each lease counts as an attempt.
- fail: bounded retries; after max_attempts the job is terminally `failed`.
Callers commit.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.logging_config import safe_error
from app.models import ResearchJob

KINDS = ("INVESTIGATE", "REVIEW_POSITION")
STATUSES = ("queued", "leased", "done", "failed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(db: Session, *, kind: str, symbol: str, job_key: str, payload: dict | None = None,
            signal_event_ids: list | None = None, decision_id: int | None = None,
            max_attempts: int | None = None) -> int | None:
    """Queue a job. Returns its id, or None when a job with this key already exists."""
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}")
    stmt = pg_insert(ResearchJob).values(
        job_key=job_key, kind=kind, symbol=symbol.upper(), signal_event_ids=signal_event_ids or [],
        decision_id=decision_id, status="queued", attempts=0,
        max_attempts=max_attempts or settings.watcher_job_max_attempts, payload=payload or {},
    ).on_conflict_do_nothing(index_elements=["job_key"]).returning(ResearchJob.id)
    return db.execute(stmt).scalar_one_or_none()


def lease(db: Session, worker: str, *, kinds: tuple[str, ...] | None = None, limit: int = 1,
          lease_seconds: int | None = None, now: datetime | None = None) -> list[ResearchJob]:
    """Lease up to `limit` available jobs (queued, or leased with an expired lease)."""
    now = now or _now()
    secs = lease_seconds or settings.watcher_job_lease_seconds
    q = (select(ResearchJob)
         .where(or_(ResearchJob.status == "queued",
                    (ResearchJob.status == "leased") & (ResearchJob.lease_until < now)))
         .order_by(ResearchJob.id).limit(limit).with_for_update(skip_locked=True))
    if kinds:
        q = q.where(ResearchJob.kind.in_(kinds))
    leased = []
    for job in db.scalars(q).all():
        if job.attempts >= job.max_attempts:  # lease expired after the last allowed attempt
            job.status = "failed"
            job.lease_until = None
            job.leased_by = None
            job.last_error = job.last_error or "lease expired after max attempts"
            continue
        job.status = "leased"
        job.attempts += 1
        job.leased_by = worker
        job.lease_until = now + timedelta(seconds=secs)
        leased.append(job)
    db.flush()
    return leased


def complete(db: Session, job: ResearchJob) -> None:
    job.status = "done"
    job.lease_until = None
    job.leased_by = None
    db.flush()


def fail(db: Session, job: ResearchJob, error: object) -> str:
    """Record a failed attempt: back to `queued` while attempts remain, else `failed`."""
    job.last_error = safe_error(error, 1000)
    job.lease_until = None
    job.leased_by = None
    job.status = "failed" if job.attempts >= job.max_attempts else "queued"
    db.flush()
    return job.status


def counts(db: Session) -> dict[str, int]:
    rows = dict(db.execute(select(ResearchJob.status, func.count()).group_by(ResearchJob.status)).all())
    return {s: int(rows.get(s, 0)) for s in STATUSES}
