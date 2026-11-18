"""Alert store (`system_alerts`): dedup by (kind, key) while active, auto-resolve, ack.

Callers commit. A partial unique index guarantees one non-resolved row per (kind, key).
"""
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.logging_config import redact
from app.models import SystemAlert
from app.services.ops.health import SEVERITIES, Finding

STATUSES = ("open", "acked", "resolved")
ACTIVE = ("open", "acked")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def raise_alert(db: Session, f: Finding, now: datetime | None = None) -> SystemAlert:
    """Insert, or bump an existing active alert for the same (kind, key). An acked alert stays
    acked (the operator already knows) but its count/last_seen/severity keep updating."""
    now = now or _now()
    sev = f.severity if f.severity in SEVERITIES else "warning"
    msg = redact(f.message)[:1000]
    stmt = pg_insert(SystemAlert).values(
        kind=f.kind, key=f.key, severity=sev, message=msg, details=f.details or {},
        first_seen=now, last_seen=now, count=1, status="open")
    stmt = stmt.on_conflict_do_update(
        index_elements=["kind", "key"], index_where=text("status <> 'resolved'"),
        set_={"severity": sev, "message": msg, "details": f.details or {}, "last_seen": now,
              "count": SystemAlert.count + 1}).returning(SystemAlert.id)
    alert_id = db.execute(stmt).scalar_one()
    db.expire_all()
    return db.get(SystemAlert, alert_id)


def resolve_cleared(db: Session, active: set[tuple[str, str]], kinds: set[str],
                    now: datetime | None = None) -> int:
    """Resolve open/acked alerts of the given `kinds` whose (kind, key) is not in `active`.
    Kinds whose check did not run successfully are left alone (never resolve on doubt)."""
    now = now or _now()
    rows = db.scalars(select(SystemAlert).where(SystemAlert.status.in_(ACTIVE),
                                                SystemAlert.kind.in_(kinds))).all()
    n = 0
    for a in rows:
        if (a.kind, a.key) not in active:
            a.status, a.resolved_at = "resolved", now
            n += 1
    db.flush()
    return n


def sync(db: Session, findings: list[Finding], ok_kinds: set[str], now: datetime | None = None) -> dict:
    """Raise every finding, resolve alerts for `ok_kinds` that have cleared."""
    now = now or _now()
    for f in findings:
        raise_alert(db, f, now)
    resolved = resolve_cleared(db, {(f.kind, f.key) for f in findings}, ok_kinds, now)
    return {"raised": len(findings), "resolved": resolved}


def ack(db: Session, alert_id: int, now: datetime | None = None) -> SystemAlert | None:
    """Acknowledge an open alert. Resolved/acked alerts are returned unchanged."""
    a = db.get(SystemAlert, alert_id)
    if a is not None and a.status == "open":
        a.status, a.acked_at = "acked", now or _now()
        db.flush()
    return a


def list_alerts(db: Session, status: str = "open", limit: int = 100) -> list[SystemAlert]:
    q = select(SystemAlert).order_by(SystemAlert.last_seen.desc(), SystemAlert.id.desc()).limit(limit)
    if status != "all":
        q = q.where(SystemAlert.status == status)
    return list(db.scalars(q).all())


def serialize(a: SystemAlert) -> dict:
    return {"id": a.id, "kind": a.kind, "key": a.key, "severity": a.severity,
            "message": redact(a.message), "details": a.details or {},
            "first_seen": a.first_seen.isoformat() if a.first_seen else None,
            "last_seen": a.last_seen.isoformat() if a.last_seen else None,
            "count": a.count, "status": a.status,
            "acked_at": a.acked_at.isoformat() if a.acked_at else None}
