"""Continuous-monitor visibility: status + recent monitor events + managed positions."""
from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import DB, Actor
from app.models import AuditLog, PositionManagement
from app.schemas import AutomationEventOut
from app.services import broker, monitor, position_manager

router = APIRouter(prefix="/monitor", tags=["monitor"])

MONITOR_EVENTS = [
    "monitor_trigger",
    "monitor_decision",
    "monitor_auto_sell",
    "monitor_stop_adjust",
    "monitor_ignored",
    "monitor_error",
    "position_managed_opened",
]


@router.get("/positions")
def managed_positions(db: Session = DB, _actor: str = Actor):
    """Position Manager view: live broker positions joined with AI-management state
    (trailing stop, partials taken, current thesis/confidence, last decision)."""
    try:
        positions = broker.get_positions()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Broker error: {e}")

    pm_by_symbol = {
        pm.symbol: pm
        for pm in db.scalars(
            select(PositionManagement).where(PositionManagement.is_open.is_(True))
        ).all()
    }

    out = []
    for p in positions:
        pm = pm_by_symbol.get(p["symbol"])
        entry = float(pm.entry_price) if pm else p["avg_entry_price"]
        out.append({
            "symbol": p["symbol"],
            "qty": p["qty"],
            "entry_price": entry,
            "current_price": p["current_price"],
            "pnl_pct": round(p["unrealized_plpc"], 2),
            "gain_pct": round(position_manager.gain_pct(entry, p["current_price"]), 2),
            "highest_price": float(pm.highest_price) if pm else None,
            "trailing_stop": float(pm.trailing_stop_price) if pm and pm.trailing_stop_price else None,
            "initial_stop": float(pm.initial_stop_price) if pm and pm.initial_stop_price else None,
            "current_confidence": float(pm.current_confidence) if pm and pm.current_confidence else None,
            "last_decision": pm.last_decision if pm else None,
            "exit_condition": pm.exit_condition if pm else None,
            "partials_taken": list(pm.partial_levels_taken) if pm else [],
            "current_thesis": pm.current_thesis if pm else None,
            "last_analyzed_at": pm.last_analyzed_at.isoformat() if pm and pm.last_analyzed_at else None,
            "managed": pm is not None,
        })
    return out


@router.get("/status")
def status(_actor: str = Actor):
    return monitor.get_status()


@router.get("/events", response_model=list[AutomationEventOut])
def events(db: Session = DB, _actor: str = Actor):
    rows = db.scalars(
        select(AuditLog)
        .where(AuditLog.event_type.in_(MONITOR_EVENTS))
        .order_by(AuditLog.ts.desc())
        .limit(100)
    ).all()
    return [
        AutomationEventOut(
            id=r.id, ts=r.ts, event_type=r.event_type, actor=r.actor,
            intent_id=r.intent_id, order_id=r.order_id, payload=r.payload,
        )
        for r in rows
    ]
