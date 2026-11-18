"""Run an analysis: aggregate data -> Claude -> validate + risk filter -> persist intents."""
import logging
import time
import uuid
from datetime import datetime, timezone

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
from app.logging_config import log_event, run_id_var, safe_error
from app.schemas import IntentOut, RunAnalysisRequest, RunSummaryOut
from app.services import (
    analyst,
    automation,
    batch,
    data_aggregator,
    intent_validation,
    prescreener,
    risk,
    risk_state,
    universes,
)
from app.services.execution import ExecutionError, execute_intent
from app.services.orchestrator import build_risk_input
from app.services.recommendations import clamp_position_size, dedupe_by_ticker

logger = logging.getLogger("trading.analysis")

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

    run_key = f"analysis-{uuid.uuid4().hex[:12]}"
    token = run_id_var.set(run_key)
    try:
        return _run(db, req, tickers, run_key)
    finally:
        run_id_var.reset(token)


def _run(db: Session, req: RunAnalysisRequest, tickers: list[str], run_key: str) -> RunSummaryOut:
    started = time.monotonic()
    log_event(logger, "analysis.start", tickers=tickers, universe=req.universe,
              model=settings.strategist_model, effort=settings.strategist_effort)

    # 1. Aggregate market data for ALL candidates + capture account snapshot. The snapshot
    #    time is taken BEFORE fetching, so data age is measured conservatively.
    data_as_of = datetime.now(timezone.utc)
    try:
        context, account = data_aggregator.build_context(tickers)
    except Exception as e:  # noqa: BLE001
        log_event(logger, "analysis.data_failed", logging.ERROR, error=safe_error(e))
        raise HTTPException(status_code=502, detail=f"Data aggregation failed: {e}")
    context["data_as_of"] = data_as_of.isoformat()

    # [DATA] per-ticker success/failure (never silently drop a ticker).
    for sym in tickers:
        m = context["watchlist_data"].get(sym, {})
        ind = m.get("indicators") or {}
        if ind.get("data_sufficient"):
            log_event(logger, "data.ok", ticker=sym, last_price=m.get("last_price"),
                      history_days=ind.get("history_days"))
        else:
            log_event(logger, "data.insufficient", logging.WARNING, ticker=sym,
                      reason=ind.get("note", "insufficient data"), last_price=m.get("last_price"))

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
    top_set = set(top_symbols)
    for sym in tickers:
        log_event(logger, "screen.passed" if sym in top_set else "screen.rejected", ticker=sym,
                  prescreen_top_n=settings.prescreen_top_n)

    # 3. Batch the top candidates through Claude (CLAUDE_BATCH_SIZE per call). A failed
    #    batch (refusal, truncation, API error, invalid output) is skipped and counted —
    #    it never crashes the run and never yields partial output. Each batch is
    #    HARD-restricted to its own tickers: positions are scoped to the batch and the
    #    parsed response is filtered so a run only ever produces intents for its tickers.
    usage = {"prompt_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "prompt_version": None}

    def analyze_fn(batch_symbols: list[str]) -> list:
        batch_set = set(batch_symbols)
        batch_context = {
            **context,
            # only show positions that ARE in this batch, so Claude isn't tempted to
            # act on unrelated holdings.
            "positions": [p for p in context["positions"] if p["symbol"] in batch_set],
            "watchlist_data": {s: context["watchlist_data"][s] for s in batch_symbols
                               if s in context["watchlist_data"]},
        }
        output, meta, ok = analyst.analyze_batch(batch_context, allowed_tickers=batch_set)
        usage["prompt_tokens"] += meta.get("input_tokens", 0) + meta.get("cache_read_tokens", 0) \
            + meta.get("cache_write_tokens", 0)
        usage["output_tokens"] += meta.get("output_tokens", 0)
        usage["cost_usd"] += meta.get("cost_usd") or 0.0
        usage["prompt_version"] = meta.get("prompt_version") or usage["prompt_version"]
        if not ok:
            raise RuntimeError(meta.get("error", "batch failed"))
        return list(output.intents)

    merged, batch_stats = batch.run_batched(
        top_symbols, settings.claude_batch_size, analyze_fn,
        dedupe_by_ticker, settings.max_final_recommendations,
    )
    data_age_sec = (datetime.now(timezone.utc) - data_as_of).total_seconds()

    run = AnalysisRun(
        snapshot_id=snapshot.id,
        model=settings.strategist_model,
        prompt_tokens=usage["prompt_tokens"],
        output_tokens=usage["output_tokens"],
        input_context={"run_key": run_key, "tickers_screened": len(tickers), "top_symbols": top_symbols,
                       "universe": req.universe, "effort": settings.strategist_effort,
                       "prompt_version": usage["prompt_version"],
                       "estimated_cost_usd": round(usage["cost_usd"], 6),
                       "data_as_of": data_as_of.isoformat(), "data_age_sec": round(data_age_sec, 1),
                       **batch_stats},
        raw_response={"intents": [i.model_dump() for i in merged]},
    )
    db.add(run)
    db.flush()

    # 4. Validate every merged intent (fail closed), then risk-filter and persist.
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
        output_violations = intent_validation.validate_intent(
            intent_dict, last_price=market.get("last_price"), data_age_sec=data_age_sec,
            max_data_age_sec=settings.max_data_age_sec,
            max_limit_deviation_pct=float(settings.max_limit_deviation_pct),
        )
        ri = build_risk_input(intent_dict, account, held_symbols, halted, market)
        result = risk.evaluate(ri)
        violations = output_violations + result.as_violation_dicts()
        passed = result.passed and not output_violations

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
            risk_passed=passed,
            risk_violations=violations,
            computed_qty=result.computed_qty if passed else None,
            computed_notional=(
                float(result.computed_notional) if passed and result.computed_notional else None
            ),
        )

        if ti.action in ("watch", "avoid"):
            # Non-actionable classifications: surfaced for the user, never executed.
            # This is a valid analysis RESULT (not a failure) and is persisted/displayed.
            row.status = IntentStatus.recommended
        elif passed:
            row.status = IntentStatus.pending_approval
        else:
            row.status = IntentStatus.rejected_by_risk

        db.add(row)
        db.flush()
        out_intents.append(row)

        log_event(logger, "validation.result", intent_id=row.id, ticker=ti.ticker, decision=ti.action,
                  confidence=round(float(ti.confidence or 0), 4), status=row.status.value,
                  risk_passed=passed, violations=[v["code"] for v in violations])

        # Audit: recommendation, plus rejection if validation or the risk engine blocked it
        audit(db, "recommendation", "claude", intent_id=row.id,
              payload={"intent": intent_dict, "risk_passed": passed, "run_key": run_key,
                       "model": settings.strategist_model, "prompt_version": usage["prompt_version"],
                       **({"position_size_clamped_from": original_size,
                           "position_size_clamped_to": clamped_size} if size_clamped else {})})
        if not passed and ti.action not in ("watch", "avoid"):
            audit(db, "risk_rejection", "risk_engine", intent_id=row.id,
                  payload={"violations": violations})

    db.commit()

    # 5. Optional autonomous execution, decided by the single automation gate: env
    #    capability + mode "auto" + kill switch off. In observe mode the same eligibility
    #    logic runs but only records what WOULD have executed. In manual mode (the
    #    default) nothing is automated and every intent waits for a human.
    gate = automation.order_gate(db)
    if gate.allowed or gate.observe:
        _auto_execute_eligible(db, out_intents, observe=gate.observe)
    else:
        for row in out_intents:
            log_event(logger, "auto_trade.skipped", ticker=row.ticker, reason=gate.reason)

    log_event(logger, "analysis.complete", tickers_screened=len(tickers), sent_to_claude=len(top_symbols),
              batches=batch_stats["batches"], failed_batches=batch_stats["failed_batches"],
              final_recommendations=len(out_intents), tokens_in=usage["prompt_tokens"],
              tokens_out=usage["output_tokens"], estimated_cost_usd=round(usage["cost_usd"], 6),
              elapsed_ms=int((time.monotonic() - started) * 1000),
              decisions=[(i.ticker, i.action.value, i.status.value) for i in out_intents])
    return RunSummaryOut(
        tickers_screened=len(tickers),
        prescreen_top_n=len(top_symbols),
        batches=batch_stats["batches"],
        failed_batches=batch_stats["failed_batches"],
        final_recommendations=len(out_intents),
        universe=req.universe,
        intents=[IntentOut.model_validate(i) for i in out_intents],
    )


def _auto_execute_eligible(db: Session, intents: list[TradeIntent], *, observe: bool = False) -> None:
    """Execute eligible intents, or (observe=True) only record that they would execute."""
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
            log_event(logger, "auto_trade.skipped", ticker=row.ticker, reason=why, category=category)
            continue
        if observe:
            audit(db, "observe_would_execute", "automation", intent_id=row.id,
                  payload={"ticker": row.ticker, "action": row.action.value,
                           "confidence": round(confidence_pct, 1), "required_threshold": min_conf,
                           "limit_price": float(row.limit_price or 0), "computed_qty": row.computed_qty})
            db.commit()
            log_event(logger, "auto_trade.observed", ticker=row.ticker, intent_id=row.id,
                      action=row.action.value, confidence=round(confidence_pct, 1))
            continue
        try:
            execute_intent(db, row, actor="automation", auto=True)
            db.commit()
            log_event(logger, "auto_trade.executed", ticker=row.ticker, intent_id=row.id)
        except ExecutionError as e:
            audit(db, "auto_trade_rejected", "automation", intent_id=row.id,
                  payload={"ticker": row.ticker, "confidence": round(confidence_pct, 1),
                           "required_threshold": min_conf, "reason": str(e.detail),
                           "rejected_by": "risk_engine" if e.violations else "execution",
                           "violations": e.violations})
            db.commit()
            log_event(logger, "auto_trade.rejected", ticker=row.ticker, reason=str(e.detail))


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
