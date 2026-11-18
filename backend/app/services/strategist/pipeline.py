"""Process ONE leased bundle: prompt -> Opus -> validate -> store proposal -> (maybe) pending intent.

Claude and the broker are injected so tests use fakes. The broker is only read (account and
positions); nothing here submits, approves or cancels an order.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import audit
from app.logging_config import log_event, safe_error
from app.models import AnalystReportRow, ResearchBundle, StrategyProposal
from app.services import llm, risk_state
from app.services.research.contracts import StrategistProposal
from app.services.strategist import STRATEGY_ID, budget, convert, prompt, validate

logger = logging.getLogger("trading.strategist")
ACTOR = "system:strategist"


class BudgetExceeded(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _call_llm(system: str, user: str):
    return llm.call_structured(role="strategist", prompt_name=prompt.PROMPT_NAME, system=system,
                               user=user, output_model=StrategistProposal)


def view_of(bundle: ResearchBundle) -> validate.BundleView:
    return validate.BundleView(
        symbol=bundle.symbol, as_of=validate._utc(bundle.as_of), expires_at=bundle.expires_at,
        evidence=bundle.evidence or {}, holding=bool(bundle.holding),
        market_snapshot_id=bundle.market_snapshot_id)


def _finish_bundle(bundle: ResearchBundle, status: str, error: str | None = None) -> None:
    bundle.status = status
    bundle.error = error
    bundle.lease_until = None
    bundle.leased_by = None


def process_bundle(db: Session, bundle: ResearchBundle, *, call_fn=None, get_account=None,
                   get_positions=None, now: datetime | None = None) -> dict:
    """Returns a summary dict. Raises BudgetExceeded (bundle untouched) when over budget."""
    now = now or datetime.now(timezone.utc)
    call_fn = call_fn or _call_llm
    if db.scalar(select(StrategyProposal.id).where(StrategyProposal.bundle_id == bundle.id).limit(1)):
        _finish_bundle(bundle, "strategized")  # a proposal already exists: never call Opus twice
        return {"bundle_id": bundle.id, "outcome": "already_proposed"}

    rows = list(db.scalars(select(AnalystReportRow).where(AnalystReportRow.bundle_id == bundle.id)
                           .order_by(AnalystReportRow.analyst)))
    n_ok = sum(1 for r in rows if r.status == "ok" and r.report)
    if n_ok < settings.strategist_min_reports:
        err = f"only {n_ok} usable analyst report(s); need {settings.strategist_min_reports}"
        _finish_bundle(bundle, "failed", err)
        audit(db, "strategist.bundle_failed", ACTOR, payload={"bundle_id": bundle.id, "error": err})
        return {"bundle_id": bundle.id, "outcome": "failed", "error": err}

    system = prompt.system_prompt()
    version = prompt.prompt_version()
    user, info = prompt.build_user_message(bundle, rows, prompt.strategy_history(db), now)
    ok, reason, _bud = budget.check(db, len(system) + len(user), now)
    if not ok:
        raise BudgetExceeded(reason or "budget")

    try:
        result = call_fn(system, user)
    except llm.LLMError as e:
        err = f"{e.kind}: {e.detail}"[:500]
        _finish_bundle(bundle, "failed", err)
        audit(db, "strategist.bundle_failed", ACTOR, payload={"bundle_id": bundle.id, "error": err})
        return {"bundle_id": bundle.id, "outcome": "failed", "error": err}
    p: StrategistProposal = result.output
    meta = getattr(result, "meta", {}) or {}
    model = meta.get("served_model") or meta.get("requested_model") or llm.get_role("strategist").model

    bview = view_of(bundle)
    vres = validate.validate(
        p, bview, now=now, max_data_age_sec=settings.max_data_age_sec,
        max_limit_deviation_pct=float(settings.max_limit_deviation_pct),
        max_position_pct=float(settings.max_position_pct))
    expires_at = convert.default_expiry(bundle.expires_at, now)
    row = StrategyProposal(
        bundle_id=bundle.id, symbol=bundle.symbol, action=p.action, status="proposed",
        strategy_id=STRATEGY_ID, strategy_version=version, model=model, confidence=p.confidence,
        quote_ts=vres.quote_ts, expires_at=expires_at, proposal=p.model_dump(),
        validation={**vres.as_dict(), "reports_used": info["reports_used"],
                    "reports_unavailable": info["reports_unavailable"]})
    db.add(row)
    db.flush()

    if vres.status == "abstained":
        row.status = "abstained"
    elif vres.status == "rejected":
        row.status = "rejected"
    else:
        try:
            account = (get_account or _broker_account)()
            positions = (get_positions or _broker_positions)()
            account["open_positions"] = len(positions)
            held = {x["symbol"].upper() for x in positions}
            halted, _ = risk_state.is_halted(db, account)
            with db.begin_nested():  # savepoint: a failed conversion leaves no half-written intent
                intent = convert.convert(
                    db, proposal=p, vres=vres, bundle=bview, account=account, held_symbols=held,
                    halted=halted, prompt_version=version, model=model, expires_at=expires_at, now=now)
            row.intent_id = intent.id
            row.status = "converted"
            row.validation = {**row.validation, "intent_status": intent.status.value,
                              "risk_passed": intent.risk_passed, "risk_violations": intent.risk_violations}
        except Exception as e:  # noqa: BLE001  fail closed: no account state, no intent
            row.status = "rejected"
            row.validation = {**row.validation, "violations": [
                *row.validation.get("violations", []),
                {"code": "CONVERSION_FAILED", "message": safe_error(e)}]}
            log_event(logger, "strategist.convert_failed", logging.WARNING, bundle_id=bundle.id,
                      error=safe_error(e))
    audit(db, "strategist.proposal", ACTOR, intent_id=row.intent_id, payload={
        "proposal_id": row.id, "bundle_id": bundle.id, "symbol": bundle.symbol, "action": p.action,
        "status": row.status, "prompt_version": version, "model": model,
        "violations": [v["code"] for v in row.validation.get("violations", [])]})
    _finish_bundle(bundle, "strategized")
    return {"bundle_id": bundle.id, "outcome": row.status, "proposal_id": row.id,
            "intent_id": row.intent_id}


def _broker_account() -> dict:
    from app.services import broker
    return broker.get_account()  # read-only


def _broker_positions() -> list[dict]:
    from app.services import broker
    return broker.get_positions()  # read-only
