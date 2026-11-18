"""Automation control: status, mode (manual / observe / auto), kill switch, event feed."""
from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import DB, Actor, audit
from app.models import AuditLog
from app.schemas import AutomationEventOut, AutomationModeRequest, AutomationStatusOut, KillSwitchRequest
from app.services import automation

router = APIRouter(prefix="/automation", tags=["automation"])


def _status(db: Session) -> AutomationStatusOut:
    allowed, reason = automation.auto_capability()
    state = automation.get_state(db)
    gate = automation.order_gate(db)
    return AutomationStatusOut(
        capability_allowed=allowed,
        capability_reason=reason,
        mode=state.mode,
        runtime_enabled=state.mode == "auto",
        observe=gate.observe,
        kill_switch=state.kill_switch,
        kill_switch_reason=state.kill_switch_reason,
        kill_switch_at=state.kill_switch_at,
        kill_switch_by=state.kill_switch_by,
        active=gate.allowed,
        active_reason=gate.reason,
        trading_mode=settings.trading_mode,
        is_paper=not settings.is_live,
        min_confidence=float(settings.auto_min_confidence),
        monitor_interval_sec=max(30, min(300, settings.monitor_interval_sec)),
    )


def _set_mode(db: Session, mode: str, actor: str) -> AutomationStatusOut:
    # Lock the row so a concurrent kill-switch request can't interleave with this check.
    state = automation.get_state(db, for_update=True)
    if mode == "auto":
        if state.kill_switch:
            raise HTTPException(status_code=409, detail="Release the kill switch before enabling auto mode")
        allowed, reason = automation.auto_capability()
        if not allowed:
            # Capability is gated by environment — cannot be turned on from the UI.
            raise HTTPException(status_code=403, detail=reason)
    previous = state.mode
    automation.set_mode(db, mode, actor)
    audit(db, "automation_mode_changed", actor,
          payload={"from": previous, "to": mode, "trading_mode": settings.trading_mode,
                   "is_paper": not settings.is_live})
    db.commit()
    return _status(db)


@router.get("/status", response_model=AutomationStatusOut)
def get_status(db: Session = DB, _actor: str = Actor):
    return _status(db)


@router.post("/mode", response_model=AutomationStatusOut)
def set_mode(req: AutomationModeRequest, db: Session = DB, actor: str = Actor):
    return _set_mode(db, req.mode, actor)


@router.post("/enable", response_model=AutomationStatusOut)
def enable(db: Session = DB, actor: str = Actor):
    """Deprecated alias for POST /automation/mode {"mode": "auto"}."""
    return _set_mode(db, "auto", actor)


@router.post("/disable", response_model=AutomationStatusOut)
def disable(db: Session = DB, actor: str = Actor):
    """Deprecated alias for POST /automation/mode {"mode": "manual"}."""
    return _set_mode(db, "manual", actor)


@router.post("/kill-switch", response_model=AutomationStatusOut)
def kill_switch(req: KillSwitchRequest, db: Session = DB, actor: str = Actor):
    """Engage (blocks all automated orders and new buys; mode -> manual) or release it.
    Releasing does not re-enable automation."""
    automation.set_kill_switch(db, req.engaged, actor, req.reason)
    audit(db, "kill_switch_engaged" if req.engaged else "kill_switch_released", actor,
          payload={"reason": req.reason})
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
