"""Automation control: status, enable/disable, and the autonomous event feed."""
from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import DB, Actor, audit
from app.models import AuditLog
from app.schemas import AutomationEventOut, AutomationStatusOut
from app.services import automation

router = APIRouter(prefix="/automation", tags=["automation"])


def _status(db: Session) -> AutomationStatusOut:
    allowed, reason = automation.auto_capability()
    runtime = automation.runtime_enabled(db)
    active, active_reason = automation.auto_trading_active(db)
    return AutomationStatusOut(
        capability_allowed=allowed,
        capability_reason=reason,
        runtime_enabled=runtime,
        active=active,
        active_reason=active_reason,
        trading_mode=settings.trading_mode,
        is_paper=not settings.is_live,
        min_confidence=float(settings.auto_min_confidence),
        monitor_interval_sec=max(30, min(300, settings.monitor_interval_sec)),
    )


@router.get("/status", response_model=AutomationStatusOut)
def get_status(db: Session = DB, _actor: str = Actor):
    return _status(db)


@router.post("/enable", response_model=AutomationStatusOut)
def enable(db: Session = DB, actor: str = Actor):
    allowed, reason = automation.auto_capability()
    if not allowed:
        # Capability is gated by environment — cannot be turned on from the UI.
        raise HTTPException(status_code=403, detail=reason)
    automation.set_runtime(db, True, actor)
    audit(db, "automation_enabled", actor,
          payload={"trading_mode": settings.trading_mode, "is_paper": not settings.is_live})
    db.commit()
    return _status(db)


@router.post("/disable", response_model=AutomationStatusOut)
def disable(db: Session = DB, actor: str = Actor):
    automation.set_runtime(db, False, actor)
    audit(db, "automation_disabled", actor, payload={})
    db.commit()
    return _status(db)


@router.get("/events", response_model=list[AutomationEventOut])
def events(db: Session = DB, _actor: str = Actor):
    stmt = (
        select(AuditLog)
        .where(AuditLog.event_type.in_(automation.AUTOMATION_EVENTS))
        .order_by(AuditLog.ts.desc())
        .limit(100)
    )
    cleared_before = automation.get_state(db).events_cleared_before
    if cleared_before is not None:
        stmt = stmt.where(AuditLog.ts > cleared_before)
    rows = db.scalars(stmt).all()
    return [
        AutomationEventOut(
            id=r.id, ts=r.ts, event_type=r.event_type, actor=r.actor,
            intent_id=r.intent_id, order_id=r.order_id, payload=r.payload,
        )
        for r in rows
    ]


@router.post("/events/clear")
def clear_events(db: Session = DB, actor: str = Actor):
    """Hide the current automation event feed from the UI. audit_log is preserved."""
    cleared_before = automation.clear_events(db, actor)
    db.commit()
    return {"cleared_before": cleared_before.isoformat()}
