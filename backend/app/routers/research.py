"""Read-only research dashboards: recent analysis runs and their data quality."""
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.deps import DB, Actor
from app.models import AnalysisRun, AnalystReportRow, MarketSnapshot, ResearchBundle, TradeIntent

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


# --------------------------------------------------------------------------- #
# Research bundles (Phase 5): evidence + specialist analyst reports            #
# --------------------------------------------------------------------------- #
def _report_view(r: AnalystReportRow) -> dict:
    return {"id": r.id, "analyst": r.analyst, "status": r.status, "model": r.model,
            "skill_version": r.skill_version, "report": r.report, "error": r.error,
            "created_at": r.created_at}


def _bundle_summary(b: ResearchBundle, reports: list[AnalystReportRow]) -> dict:
    return {"id": b.id, "created_at": b.created_at, "job_id": b.job_id, "symbol": b.symbol,
            "kind": b.kind, "status": b.status, "as_of": b.as_of, "holding": b.holding,
            "market_snapshot_id": b.market_snapshot_id, "ready_at": b.ready_at,
            "expires_at": b.expires_at, "error": b.error, "evidence_count": len(b.evidence or {}),
            "reports": {r.analyst: {"status": r.status, "stance": (r.report or {}).get("stance"),
                                    "confidence": (r.report or {}).get("confidence")} for r in reports}}


@router.get("/bundles")
def list_bundles(status: str | None = None, symbol: str | None = None,
                 limit: int = Query(20, ge=1, le=100), db: Session = DB, _actor: str = Actor):
    q = select(ResearchBundle).order_by(ResearchBundle.id.desc()).limit(limit)
    if status:
        q = q.where(ResearchBundle.status == status)
    if symbol:
        q = q.where(ResearchBundle.symbol == symbol.upper())
    bundles = db.scalars(q).all()
    by_bundle: dict[int, list[AnalystReportRow]] = {b.id: [] for b in bundles}
    if bundles:
        for r in db.scalars(select(AnalystReportRow).where(AnalystReportRow.bundle_id.in_(list(by_bundle)))):
            by_bundle[r.bundle_id].append(r)
    return [_bundle_summary(b, by_bundle[b.id]) for b in bundles]


@router.get("/bundles/{bundle_id}")
def get_bundle(bundle_id: int, db: Session = DB, _actor: str = Actor):
    b = db.get(ResearchBundle, bundle_id)
    if not b:
        raise HTTPException(404, "bundle not found")
    reports = db.scalars(select(AnalystReportRow).where(AnalystReportRow.bundle_id == b.id)
                         .order_by(AnalystReportRow.id)).all()
    return {**_bundle_summary(b, list(reports)), "evidence": b.evidence, "context": b.context,
            "analyst_reports": [_report_view(r) for r in reports]}
