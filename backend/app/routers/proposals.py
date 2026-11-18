"""Read-only API for strategist proposals (issue #22)."""
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import DB, Actor
from app.models import AnalystReportRow, StrategyProposal, TradeIntent

router = APIRouter(prefix="/proposals", tags=["proposals"])


def _summary(p: StrategyProposal) -> dict:
    return {"id": p.id, "created_at": p.created_at, "bundle_id": p.bundle_id, "symbol": p.symbol,
            "action": p.action, "status": p.status, "strategy_id": p.strategy_id,
            "strategy_version": p.strategy_version, "model": p.model,
            "confidence": float(p.confidence) if p.confidence is not None else None,
            "quote_ts": p.quote_ts, "expires_at": p.expires_at, "intent_id": p.intent_id}


@router.get("")
def list_proposals(status: str | None = None, symbol: str | None = None,
                   limit: int = Query(50, ge=1, le=200), db: Session = DB, _actor: str = Actor):
    q = select(StrategyProposal)
    if status:
        q = q.where(StrategyProposal.status == status)
    if symbol:
        q = q.where(StrategyProposal.symbol == symbol.upper())
    rows = db.scalars(q.order_by(StrategyProposal.id.desc()).limit(limit)).all()
    return [_summary(p) for p in rows]


@router.get("/{proposal_id}")
def get_proposal(proposal_id: int, db: Session = DB, _actor: str = Actor):
    p = db.get(StrategyProposal, proposal_id)
    if p is None:
        raise HTTPException(404, "Proposal not found")
    reports = db.scalars(select(AnalystReportRow).where(AnalystReportRow.bundle_id == p.bundle_id)
                         .order_by(AnalystReportRow.analyst)).all()
    intent = db.get(TradeIntent, p.intent_id) if p.intent_id else None
    return {
        **_summary(p),
        "proposal": p.proposal,
        "validation": p.validation,
        "reports": [{"id": r.id, "analyst": r.analyst, "status": r.status, "model": r.model,
                     "skill_version": r.skill_version, "report": r.report, "error": r.error}
                    for r in reports],
        "intent": None if intent is None else {
            "id": intent.id, "status": intent.status.value, "action": intent.action.value,
            "ticker": intent.ticker, "limit_price": float(intent.limit_price or 0),
            "position_size_percent": float(intent.position_size_percent or 0),
            "risk_passed": intent.risk_passed, "risk_violations": intent.risk_violations,
            "expires_at": intent.expires_at},
    }
