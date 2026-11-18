"""Operations API: system status snapshot and the health-watcher alerts. Read-mostly; the only
write is acknowledging an alert (audited). No broker, Claude or network access."""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.orm import Session

from app.deps import DB, Actor, audit
from app.services.ops import alerts, collect

router = APIRouter(prefix="/ops", tags=["ops"])


@router.get("/status")
def status(db: Session = DB, _actor: str = Actor):
    return collect.status_snapshot(db, datetime.now(timezone.utc))


@router.get("/alerts")
def list_alerts(status: str = Query("open", pattern="^(open|acked|resolved|all)$"),
                limit: int = Query(100, ge=1, le=500), db: Session = DB, _actor: str = Actor):
    return [alerts.serialize(a) for a in alerts.list_alerts(db, status, limit)]


@router.post("/alerts/{alert_id}/ack")
def ack_alert(alert_id: int, db: Session = DB, actor: str = Actor):
    before = db.get(alerts.SystemAlert, alert_id)
    if before is None:
        raise HTTPException(404, "alert not found")
    was_open = before.status == "open"
    alert = alerts.ack(db, alert_id)
    if was_open:
        audit(db, "system_alert_acked", actor,
              payload={"alert_id": alert_id, "kind": alert.kind, "key": alert.key})
    db.commit()
    return alerts.serialize(alert)
