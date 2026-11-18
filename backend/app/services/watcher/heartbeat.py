"""Worker heartbeats (table `worker_heartbeats`)."""
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import WorkerHeartbeat


def beat(db: Session, worker: str, status: str, details: dict, *, started_at: datetime,
         now: datetime | None = None) -> None:
    """Upsert this worker's heartbeat. Caller commits."""
    now = now or datetime.now(timezone.utc)
    stmt = pg_insert(WorkerHeartbeat).values(
        worker=worker, status=status, started_at=started_at, last_beat=now, details=details)
    db.execute(stmt.on_conflict_do_update(
        index_elements=["worker"],
        set_={"status": status, "started_at": started_at, "last_beat": now, "details": details}))


def get(db: Session, worker: str) -> WorkerHeartbeat | None:
    return db.get(WorkerHeartbeat, worker)
