"""Run an analysis: aggregate data -> Claude -> risk filter -> persist intents."""
from fastapi import APIRouter, Body, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import DB, Actor, audit
from app.models import (
    AccountSnapshot,
    AnalysisRun,
    IntentAction,
    IntentStatus,
    TradeIntent,
    Watchlist,
)
from app.config import settings
from app.schemas import IntentOut, RunAnalysisRequest, RunSummaryOut
from app.services import (
    analyst,
    automation,
    batch,
    data_aggregator,
    prescreener,
    risk,
    risk_state,
    universes,
)
from app.services.execution import ExecutionError, execute_intent
from app.services.orchestrator import build_risk_input
from app.services.recommendations import clamp_position_size, dedupe_by_ticker

router = APIRouter(prefix="/analysis", tags=["analysis"])


@router.get("/universes")
def list_universes(_actor: str = Actor):
    return universes.universe_names()


@router.post("/run", response_model=RunSummaryOut)
def run_analysis(
    req: RunAnalysisRequest = Body(default_factory=RunAnalysisRequest),
    db: Session = DB,
    actor: str = Actor,
):
    # Resolve tickers: universe > explicit request > active watchlist. Cap the size so a
    # huge watchlist can't blow up data-gathering.
    if req.universe:
        tickers = [t.upper() for t in universes.get_universe(req.universe)]
        if not tickers:
            raise HTTPException(status_code=400, detail=f"Unknown universe '{req.universe}'")
    elif req.tickers:
        tickers = [t.upper() for t in req.tickers]
    else:
        tickers = list(
            db.scalars(select(Watchlist.ticker).where(Watchlist.active.is_(True))).all()
        )
    # de-dup preserving order, then cap
    tickers = list(dict.fromkeys(tickers))[: settings.max_watchlist_size]
    if not tickers:
        raise HTTPException(status_code=400, detail="No tickers; add to the watchlist or pick a universe")

    # 1. Aggregate market data for ALL candidates + capture account snapshot.
    try:
        context, account = data_aggregator.build_context(tickers)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Data aggregation failed: {e}")

    snapshot = AccountSnapshot(
        equity=account["equity"],
        cash=account["cash"],
        buying_power=account["buying_power"],
        day_pnl=account["day_pnl"],
        day_pnl_pct=round(account["day_pnl_pct"], 4),
        open_positions=context["portfolio"]["open_positions"],
        is_paper=account["is_paper"],
        raw=account["raw"],
    )
    db.add(snapshot)
    db.flush()

    # 2. Quantitative prescreen: rank all candidates, keep only the top N for Claude.
    ranked = prescreener.rank_candidates(context["watchlist_data"], settings.prescreen_top_n)
    top_symbols = [r.symbol for r in ranked]

    # 3. Batch the top candidates through Claude (CLAUDE_BATCH_SIZE per call). A failed /
    #    malformed batch is skipped (counted), never crashes the run.
    token_meta = {"prompt_tokens": 0, "output_tokens": 0}

    def analyze_fn(batch_symbols: list[str]) -> list:
        batch_context = {
            **context,
            "watchlist_data": {s: context["watchlist_data"][s] for s in batch_symbols
                               if s in context["watchlist_data"]},
        }
        output, meta, ok = analyst.analyze_batch(batch_context)
        token_meta["prompt_tokens"] += meta.get("prompt_tokens", 0)
        token_meta["output_tokens"] += meta.get("output_tokens", 0)
        if not ok:
            raise RuntimeError(meta.get("error", "batch failed"))
        return list(output.intents)

    merged, batch_stats = batch.run_batched(
        top_symbols, settings.claude_batch_size, analyze_fn,
        dedupe_by_ticker, settings.max_final_recommendations,
    )

    run = AnalysisRun(
        snapshot_id=snapshot.id,
        model=settings.claude_model,
        prompt_tokens=token_meta["prompt_tokens"],
        output_tokens=token_meta["output_tokens"],
        input_context={"tickers_screened": len(tickers), "top_symbols": top_symbols,
                       "universe": req.universe, **batch_stats},
        raw_response={"intents": [i.model_dump() for i in merged]},
    )
    db.add(run)
    db.flush()

    # 4. Risk-filter every merged intent and persist
    account["open_positions"] = context["portfolio"]["open_positions"]
    held_symbols = {p["symbol"] for p in context["positions"]}
    halted, _reason = risk_state.is_halted(db, account)

    max_pct = float(settings.max_position_pct)
    out_intents: list[TradeIntent] = []
    for ti in merged:  # already deduped (highest confidence per ticker) + capped
        intent_dict = ti.model_dump()

        # Safeguard: clamp an over-size proposal to MAX_POSITION_PCT *before* risk
        # validation, so a too-large BUY is shrunk to the cap instead of rejected.
        original_size = float(intent_dict.get("position_size_percent") or 0)
        clamped_size = clamp_position_size(original_size, max_pct)
        size_clamped = clamped_size < original_size
        intent_dict["position_size_percent"] = clamped_size

        market = context["watchlist_data"].get(ti.ticker, {})
        ri = build_risk_input(intent_dict, account, held_symbols, halted, market)
        result = risk.evaluate(ri)

        row = TradeIntent(
            run_id=run.id,
            ticker=ti.ticker,
            action=IntentAction(ti.action),
            order_type=ti.order_type,
            limit_price=ti.limit_price,
            position_size_percent=clamped_size,  # store the clamped value, not the raw one
            stop_loss_percent=ti.stop_loss_percent,
            take_profit_percent=ti.take_profit_percent,
            confidence=ti.confidence,
            reason=ti.reason,
            risks=ti.risks,
            risk_passed=result.passed,
            risk_violations=result.as_violation_dicts(),
            computed_qty=result.computed_qty,
            computed_notional=(
                float(result.computed_notional) if result.computed_notional else None
            ),
        )

        if ti.action in ("watch", "avoid"):
            # Non-actionable classifications: surfaced for the user, never executed.
            row.status = IntentStatus.recommended
        elif result.passed:
            row.status = IntentStatus.pending_approval
        else:
            row.status = IntentStatus.rejected_by_risk

        db.add(row)
        db.flush()
        out_intents.append(row)

        # Audit: recommendation, plus rejection if the risk engine blocked it
        audit(db, "recommendation", "claude", intent_id=row.id,
              payload={"intent": intent_dict, "risk_passed": result.passed,
                       **({"position_size_clamped_from": original_size,
                           "position_size_clamped_to": clamped_size} if size_clamped else {})})
        if not result.passed:
            audit(db, "risk_rejection", "risk_engine", intent_id=row.id,
                  payload={"violations": result.as_violation_dicts()})

    db.commit()

    # 5. Optional autonomous execution. Only runs when automation is ACTIVE
    #    (env capability + live confirmation when live + runtime switch ON). When
    #    inactive — the default — this is a no-op and everything stays manual.
    active, _why = automation.auto_trading_active(db)
    if active:
        _auto_execute_eligible(db, out_intents)

    return RunSummaryOut(
        tickers_screened=len(tickers),
        prescreen_top_n=len(top_symbols),
        batches=batch_stats["batches"],
        failed_batches=batch_stats["failed_batches"],
        final_recommendations=len(out_intents),
        universe=req.universe,
        intents=[IntentOut.model_validate(i) for i in out_intents],
    )


def _auto_execute_eligible(db: Session, intents: list[TradeIntent]) -> None:
    min_conf = float(settings.auto_min_confidence)
    for row in intents:
        if row.status != IntentStatus.pending_approval:
            continue  # watch/avoid and risk-rejected intents are never auto-executed
        confidence_pct = float(row.confidence or 0) * 100
        ok, why, category = automation.should_auto_execute(
            action=row.action.value,
            confidence_pct=confidence_pct,
            stop_loss_pct=float(row.stop_loss_percent or 0),
            take_profit_pct=float(row.take_profit_percent or 0),
            risk_passed=bool(row.risk_passed),
            min_confidence=min_conf,
        )
        if not ok:
            audit(db, "auto_trade_rejected", "automation", intent_id=row.id,
                  payload={"ticker": row.ticker, "confidence": round(confidence_pct, 1),
                           "required_threshold": min_conf, "reason": why,
                           "rejected_by": category})
            db.commit()
            continue
        try:
            execute_intent(db, row, actor="automation", auto=True)
            db.commit()
        except ExecutionError as e:
            audit(db, "auto_trade_rejected", "automation", intent_id=row.id,
                  payload={"ticker": row.ticker, "confidence": round(confidence_pct, 1),
                           "required_threshold": min_conf, "reason": str(e.detail),
                           "rejected_by": "risk_engine", "violations": e.violations})
            db.commit()


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = DB, _actor: str = Actor):
    run = db.get(AnalysisRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return {
        "id": run.id,
        "created_at": run.created_at,
        "model": run.model,
        "prompt_tokens": run.prompt_tokens,
        "output_tokens": run.output_tokens,
        "input_context": run.input_context,
        "raw_response": run.raw_response,
    }
