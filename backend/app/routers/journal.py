"""Trade journal: realized trade outcomes, hypothetical candidate outcomes, manual sync.
Everything here is read-only with respect to the broker (order sync only reads)."""
from fastapi import APIRouter
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.deps import DB, Actor, audit
from app.models import AnalysisRun, CandidateOutcome, TradeIntent, TradeOutcome
from app.services import order_sync, outcomes

router = APIRouter(prefix="/journal", tags=["journal"])


def _f(v):
    return float(v) if v is not None else None


@router.get("/trades")
def list_trades(limit: int = 200, db: Session = DB, _actor: str = Actor):
    """Realized/open trade outcomes with the intent, run and market snapshot they came from."""
    limit = max(1, min(limit, 1000))
    rows = db.execute(
        select(TradeOutcome, TradeIntent, AnalysisRun.market_snapshot_id)
        .join(TradeIntent, TradeIntent.id == TradeOutcome.intent_id)
        .join(AnalysisRun, AnalysisRun.id == TradeIntent.run_id, isouter=True)
        .order_by(TradeOutcome.id.desc()).limit(limit)
    ).all()
    return [
        {
            "id": o.id, "intent_id": o.intent_id, "run_id": i.run_id,
            "market_snapshot_id": i.market_snapshot_id or run_snapshot_id,
            "symbol": o.symbol, "side": i.action.value,
            "strategy_id": i.strategy_id, "strategy_version": i.strategy_version,
            "prompt_version": i.prompt_version, "status": o.status,
            "entry_order_id": o.entry_order_id, "exit_order_id": o.exit_order_id,
            "entry_ts": o.entry_ts, "entry_price": _f(o.entry_price),
            "exit_ts": o.exit_ts, "exit_price": _f(o.exit_price), "qty": o.qty,
            "realized_pnl": _f(o.realized_pnl), "realized_pnl_pct": _f(o.realized_pnl_pct),
            "holding_days": o.holding_days, "exit_reason": o.exit_reason,
            "benchmark_symbol": o.benchmark_symbol,
            "benchmark_return_pct": _f(o.benchmark_return_pct),
            "computed_at": o.computed_at,
        }
        for o, i, run_snapshot_id in rows
    ]


@router.get("/candidates")
def candidate_summary(db: Session = DB, _actor: str = Actor):
    """Hypothetical forward returns aggregated by intent status, action and horizon."""
    excess = CandidateOutcome.return_pct - CandidateOutcome.benchmark_return_pct
    rows = db.execute(
        select(
            TradeIntent.status, TradeIntent.action, CandidateOutcome.horizon_days,
            CandidateOutcome.rule_version,
            func.count().label("n"),
            func.avg(CandidateOutcome.return_pct),
            func.avg(CandidateOutcome.benchmark_return_pct),
            func.avg(excess),
            func.avg(case((CandidateOutcome.return_pct > 0, 1.0), else_=0.0)),
        )
        .join(TradeIntent, TradeIntent.id == CandidateOutcome.intent_id)
        .group_by(TradeIntent.status, TradeIntent.action, CandidateOutcome.horizon_days,
                  CandidateOutcome.rule_version)
        .order_by(TradeIntent.status, TradeIntent.action, CandidateOutcome.horizon_days)
    ).all()
    return [
        {"status": s.value, "action": a.value, "horizon_days": h, "rule_version": rv, "count": n,
         "avg_return_pct": _f(r), "avg_benchmark_return_pct": _f(b), "avg_excess_return_pct": _f(x),
         "hit_rate": _f(w)}
        for s, a, h, rv, n, r, b, x, w in rows
    ]


@router.post("/sync")
def sync_journal(db: Session = DB, actor: str = Actor):
    """Run the read-only order sync and compute any missing candidate outcomes."""
    order_result = order_sync.sync_orders(db)
    candidate_result = outcomes.compute_candidate_outcomes(db)
    audit(db, "journal_sync", actor, payload={"orders": order_result, "candidates": candidate_result})
    db.commit()
    return {"orders": order_result, "candidates": candidate_result}
