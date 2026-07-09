"""Trade intents: list, approve (manual gate -> broker), reject, and history mgmt
(archive/clear). Archiving hides intents from the UI without a destructive delete."""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import DB, Actor, audit
from app.models import IntentStatus, TradeIntent
from app.schemas import IntentOut, RejectRequest
from app.services.execution import ExecutionError, execute_intent

router = APIRouter(prefix="/intents", tags=["intents"])

# "Dead" statuses that the "clear rejected" action archives. Pending/actionable intents
# are never bulk-archived here.
_REJECTED_STATUSES = [
    IntentStatus.rejected_by_risk,
    IntentStatus.rejected_by_user,
    IntentStatus.order_failed,
]


@router.get("", response_model=list[IntentOut])
def list_intents(status: str | None = None, db: Session = DB, _actor: str = Actor):
    # Archived intents are hidden from every list by default.
    stmt = (
        select(TradeIntent)
        .where(TradeIntent.archived_at.is_(None))
        .order_by(TradeIntent.created_at.desc())
    )
    if status:
        stmt = stmt.where(TradeIntent.status == IntentStatus(status))
    rows = db.scalars(stmt.limit(200)).all()
    return [IntentOut.model_validate(r) for r in rows]


# --- history management (define before /{intent_id}/* to avoid path ambiguity) ---
@router.post("/clear-rejected")
def clear_rejected(db: Session = DB, actor: str = Actor):
    """Archive all rejected/failed intents (hidden from UI; audit preserved)."""
    rows = db.scalars(
        select(TradeIntent).where(
            TradeIntent.status.in_(_REJECTED_STATUSES),
            TradeIntent.archived_at.is_(None),
        )
    ).all()
    now = datetime.now(timezone.utc)
    for r in rows:
        r.archived_at = now
    audit(db, "intents_cleared", actor, payload={"kind": "rejected", "count": len(rows)})
    db.commit()
    return {"archived": len(rows)}


@router.post("/clear-watch")
def clear_watch(db: Session = DB, actor: str = Actor):
    """Archive all watch/avoid (status=recommended) intents."""
    rows = db.scalars(
        select(TradeIntent).where(
            TradeIntent.status == IntentStatus.recommended,
            TradeIntent.archived_at.is_(None),
        )
    ).all()
    now = datetime.now(timezone.utc)
    for r in rows:
        r.archived_at = now
    audit(db, "intents_cleared", actor, payload={"kind": "watch", "count": len(rows)})
    db.commit()
    return {"archived": len(rows)}


@router.delete("/{intent_id}", status_code=204)
def delete_intent(intent_id: int, db: Session = DB, actor: str = Actor):
    """Archive a single intent (non-destructive — the row and its audit trail remain)."""
    intent = db.get(TradeIntent, intent_id)
    if not intent:
        raise HTTPException(status_code=404, detail="Intent not found")
    intent.archived_at = datetime.now(timezone.utc)
    audit(db, "intent_archived", actor, intent_id=intent.id,
          payload={"status": intent.status.value})
    db.commit()
    return Response(status_code=204)


@router.post("/{intent_id}/reject", response_model=IntentOut)
def reject_intent(intent_id: int, req: RejectRequest, db: Session = DB, actor: str = Actor):
    intent = db.get(TradeIntent, intent_id)
    if not intent:
        raise HTTPException(status_code=404, detail="Intent not found")
    if intent.status != IntentStatus.pending_approval:
        raise HTTPException(status_code=409, detail=f"Intent is {intent.status.value}, not pending")

    intent.status = IntentStatus.rejected_by_user
    audit(db, "user_rejection", actor, intent_id=intent.id, payload={"reason": req.reason})
    db.commit()
    return IntentOut.model_validate(intent)


@router.post("/{intent_id}/approve", response_model=IntentOut)
def approve_intent(intent_id: int, db: Session = DB, actor: str = Actor):
    """The manual approval gate. Re-validates risk against FRESH state, then submits a
    bracket order to the broker. Still the primary human path from intent to order —
    autonomous trading is additive and does not replace this."""
    intent = db.get(TradeIntent, intent_id)
    if not intent:
        raise HTTPException(status_code=404, detail="Intent not found")
    if intent.status != IntentStatus.pending_approval:
        raise HTTPException(
            status_code=409, detail=f"Intent is {intent.status.value}, not awaiting approval"
        )

    try:
        execute_intent(db, intent, actor, auto=False)
    except ExecutionError as e:
        db.commit()  # persist the recorded rejection/failure state + audit rows
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    db.commit()
    return IntentOut.model_validate(intent)
