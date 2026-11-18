"""Turn an accepted strategist proposal into a normal TradeIntent that waits for approval.

The intent goes through the same intent_validation + risk engine as every other intent and
lands as `pending_approval` or `rejected_by_risk`. Nothing here executes, approves, or talks
to the broker: account/position data is passed in by the caller.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.deps import audit
from app.models import AnalysisRun, IntentAction, IntentStatus, TradeIntent
from app.services import intent_validation, risk
from app.services.orchestrator import build_risk_input
from app.services.research.contracts import StrategistProposal
from app.services.strategist import STRATEGY_ID
from app.services.strategist.validate import BundleView, ValidationResult

ACTOR = "system:strategist"


def intent_fields(p: StrategistProposal, size_pct: float) -> dict:
    """Limit price = midpoint of the entry range; stop/take percents derive from the
    invalidation / take-profit prices relative to that limit (mirrored for SELL)."""
    limit = round((p.entry_low + p.entry_high) / 2, 4)
    if p.action == "BUY":
        stop = (limit - p.invalidation_price) / limit * 100
        take = (p.take_profit_price - limit) / limit * 100
    else:
        stop = (p.invalidation_price - limit) / limit * 100
        take = (limit - p.take_profit_price) / limit * 100
    return {
        "ticker": p.symbol.upper(), "action": p.action.lower(), "order_type": "limit",
        "limit_price": limit, "position_size_percent": size_pct,
        "stop_loss_percent": round(stop, 4), "take_profit_percent": round(take, 4),
        "confidence": p.confidence, "reason": p.thesis,
        "risks": "; ".join([*p.risks, *[f"limitation: {x}" for x in p.limitations]]),
        "evidence": list(p.evidence_ids),
    }


def convert(db: Session, *, proposal: StrategistProposal, vres: ValidationResult, bundle: BundleView,
            account: dict, held_symbols: set[str], halted: bool, prompt_version: str, model: str | None,
            expires_at: datetime, now: datetime | None = None) -> TradeIntent:
    """Create the intent row (flushed, not committed). Caller sets proposal status from it."""
    now = now or datetime.now(timezone.utc)
    fields = intent_fields(proposal, vres.position_size_percent)
    market = {"last_price": vres.quote_price, "intraday_change_pct": 0}
    data_age = (now - (vres.quote_ts or bundle.as_of)).total_seconds()
    quality = {"stale": data_age > settings.max_data_age_sec, "incomplete": vres.quote_price is None,
               "quote_age_sec": round(data_age), "missing": [], "source": "research_bundle"}
    out_violations = intent_validation.validate_intent(
        fields, last_price=vres.quote_price, data_age_sec=data_age,
        max_data_age_sec=settings.max_data_age_sec,
        max_limit_deviation_pct=float(settings.max_limit_deviation_pct), quality=quality)
    if fields["action"] == "sell" and fields["ticker"] not in held_symbols:
        out_violations.append({"code": "NO_POSITION_TO_SELL",
                               "message": f"{fields['ticker']} is not currently held"})
    result = risk.evaluate(build_risk_input(fields, account, held_symbols, halted, market))
    violations = out_violations + result.as_violation_dicts()
    passed = result.passed and not out_violations

    run = AnalysisRun(
        market_snapshot_id=bundle.market_snapshot_id, model=model or settings.strategist_model,
        input_context={"source": "strategist", "strategy_id": STRATEGY_ID,
                       "prompt_version": prompt_version, "symbol": bundle.symbol},
        raw_response={"proposal": proposal.model_dump()})
    db.add(run)
    db.flush()
    row = TradeIntent(
        run_id=run.id, ticker=fields["ticker"], action=IntentAction(fields["action"]),
        order_type="limit", limit_price=fields["limit_price"],
        position_size_percent=fields["position_size_percent"],
        stop_loss_percent=fields["stop_loss_percent"], take_profit_percent=fields["take_profit_percent"],
        confidence=fields["confidence"], reason=fields["reason"], risks=fields["risks"],
        strategy_id=STRATEGY_ID, strategy_version=prompt_version, prompt_version=prompt_version,
        evidence=fields["evidence"], market_snapshot_id=bundle.market_snapshot_id,
        expires_at=expires_at, risk_passed=passed, risk_violations=violations,
        computed_qty=result.computed_qty if passed else None,
        computed_notional=float(result.computed_notional) if passed and result.computed_notional else None,
        status=IntentStatus.pending_approval if passed else IntentStatus.rejected_by_risk)
    db.add(row)
    db.flush()
    audit(db, "recommendation", ACTOR, intent_id=row.id,
          payload={"intent": fields, "risk_passed": passed, "strategy_id": STRATEGY_ID,
                   "model": model, "prompt_version": prompt_version})
    if not passed:
        audit(db, "risk_rejection", "risk_engine", intent_id=row.id, payload={"violations": violations})
    return row


def default_expiry(bundle_expires_at: datetime | None, now: datetime) -> datetime:
    exp = now + timedelta(minutes=settings.intent_max_age_min)
    if bundle_expires_at is not None:
        b = bundle_expires_at if bundle_expires_at.tzinfo else bundle_expires_at.replace(tzinfo=timezone.utc)
        exp = min(exp, b)
    return exp
