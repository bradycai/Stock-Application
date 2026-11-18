"""Review reports and strategy metrics (read-only), plus recording a human decision on a
report. The decision changes nothing else: no setting, strategy or order is touched."""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import DB, Actor, audit
from app.models import ReviewReport, StrategyMetric

router = APIRouter(tags=["reviews"])


class Decision(BaseModel):
    decision: Literal["approve", "reject"]


def _report(r: ReviewReport, full: bool) -> dict:
    out = {
        "id": r.id, "kind": r.kind, "created_at": r.created_at, "period_start": r.period_start,
        "period_end": r.period_end, "summary": r.summary,
        "approval_status": (r.data or {}).get("approval_status", "pending"),
        "suggested_changes": (r.data or {}).get("suggested_changes", []),
        "errors": (r.data or {}).get("errors", []),
    }
    if full:
        out["data"] = r.data
        out["llm_run_id"] = r.llm_run_id
    return out


@router.get("/reviews")
def list_reviews(period: str | None = None, limit: int = 30, db: Session = DB, _actor: str = Actor):
    q = select(ReviewReport).order_by(ReviewReport.period_start.desc(), ReviewReport.id.desc())
    if period:
        q = q.where(ReviewReport.kind == period)
    return [_report(r, False) for r in db.execute(q.limit(max(1, min(limit, 200)))).scalars()]


@router.get("/reviews/{report_id}")
def get_review(report_id: int, db: Session = DB, _actor: str = Actor):
    r = db.get(ReviewReport, report_id)
    if r is None:
        raise HTTPException(404, "Review not found")
    return _report(r, True)


@router.get("/strategy-metrics")
def list_strategy_metrics(strategy_id: str | None = None, min_sample: int | None = None, limit: int = 500,
                          db: Session = DB, _actor: str = Actor):
    q = select(StrategyMetric).order_by(StrategyMetric.window_end.desc(), StrategyMetric.id)
    if strategy_id:
        q = q.where(StrategyMetric.strategy_id == strategy_id)
    out = []
    for m in db.execute(q.limit(max(1, min(limit, 2000)))).scalars():
        if min_sample is not None and (m.metrics or {}).get("sample_size", 0) < min_sample:
            continue
        out.append({"id": m.id, "strategy_id": m.strategy_id, "strategy_version": m.strategy_version,
                    "window_start": m.window_start, "window_end": m.window_end, "metrics": m.metrics,
                    "computed_at": m.computed_at})
    return out


@router.post("/reviews/{report_id}/decision")
def decide(report_id: int, body: Decision, db: Session = DB, actor: str = Actor):
    """Record the human decision only; it does not apply any suggested change."""
    r = db.get(ReviewReport, report_id)
    if r is None:
        raise HTTPException(404, "Review not found")
    status = "approved" if body.decision == "approve" else "rejected"
    data = dict(r.data or {})
    data.update(approval_status=status, decided_by=actor, decided_at=datetime.now(timezone.utc).isoformat())
    r.data = data  # reassign so the JSONB change is detected
    audit(db, "review_decision", actor, payload={"review_id": r.id, "decision": status})
    db.commit()
    return _report(r, False)
