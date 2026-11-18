"""Read-only research dashboards: recent analysis runs and their data quality."""
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.deps import DB, Actor
from app.models import AnalysisRun, MarketSnapshot, TradeIntent

router = APIRouter(prefix="/research", tags=["research"])


def _quality_counts(quality: dict | None) -> dict:
    stale = incomplete = 0
    for q in (quality or {}).values():
        if not isinstance(q, dict):
            continue
        stale += bool(q.get("stale"))
        incomplete += bool(q.get("incomplete"))
    return {"stale": stale, "incomplete": incomplete, "symbols": len(quality or {})}


@router.get("/runs")
def list_runs(limit: int = Query(20, ge=1, le=100), db: Session = DB, _actor: str = Actor):
    runs = db.scalars(select(AnalysisRun).order_by(AnalysisRun.id.desc()).limit(limit)).all()
    ids = [r.id for r in runs]
    intent_counts: dict[int, dict] = {i: {} for i in ids}
    if ids:
        rows = db.execute(
            select(TradeIntent.run_id, TradeIntent.status, func.count())
            .where(TradeIntent.run_id.in_(ids)).group_by(TradeIntent.run_id, TradeIntent.status)
        ).all()
        for run_id, status, n in rows:
            intent_counts[run_id][status.value] = n
    snap_ids = [r.market_snapshot_id for r in runs if r.market_snapshot_id]
    quality = {}
    if snap_ids:
        quality = dict(db.execute(
            select(MarketSnapshot.id, MarketSnapshot.quality).where(MarketSnapshot.id.in_(snap_ids))
        ).all())
    out = []
    for r in runs:
        ctx = r.input_context or {}
        out.append({
            "id": r.id, "created_at": r.created_at, "model": r.model,
            "effort": ctx.get("effort"), "prompt_version": ctx.get("prompt_version"),
            "estimated_cost_usd": ctx.get("estimated_cost_usd"),
            "tickers_screened": ctx.get("tickers_screened"),
            "batches": ctx.get("batches"), "failed_batches": ctx.get("failed_batches"),
            "data_age_sec": ctx.get("data_age_sec"),
            "market_snapshot_id": r.market_snapshot_id,
            "quality": _quality_counts(quality.get(r.market_snapshot_id)) if r.market_snapshot_id else None,
            "intents_by_status": intent_counts.get(r.id, {}),
        })
    return out


@router.get("/runs/{run_id}/quality")
def run_quality(run_id: int, db: Session = DB, _actor: str = Actor):
    run = db.get(AnalysisRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    snap = db.get(MarketSnapshot, run.market_snapshot_id) if run.market_snapshot_id else None
    if not snap:
        return {"run_id": run_id, "market_snapshot_id": None, "symbols": []}
    symbols = []
    for sym, q in sorted((snap.quality or {}).items()):
        q = q if isinstance(q, dict) else {}
        symbols.append({
            "symbol": sym, "stale": bool(q.get("stale")), "incomplete": bool(q.get("incomplete")),
            "missing": q.get("missing") or [], "quote_age_sec": q.get("quote_age_sec"),
            "no_quality_data": not q,
        })
    return {"run_id": run_id, "market_snapshot_id": snap.id, "symbols": symbols}
