"""Shared intent -> order execution.

Both the manual approval endpoint and the autonomous path call execute_intent(). It
re-validates risk against FRESH state, submits a bracket order, records the order, and
writes audit rows. It does NOT commit — the caller owns the transaction so it can decide
how to surface success/failure.

Keeping this in one place means manual approval and auto-trading share the exact same
risk re-validation and bracket-order submission. Manual approval behavior is unchanged.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import audit
from app.logging_config import log_event, safe_error
from app.models import IntentStatus, Order, TradeIntent
from app.services import (
    automation,
    broker,
    intent_validation,
    market_data,
    risk,
    risk_policies,
    risk_policy_data,
    risk_state,
    snapshots,
)
from app.services.orchestrator import build_risk_input
from app.services.recommendations import clamp_position_size

logger = logging.getLogger("trading.execution")


class ExecutionError(Exception):
    """Raised when an intent cannot be executed. Carries an HTTP status + detail and the
    risk violations (if any). The caller must still commit to persist the recorded state."""

    def __init__(self, status_code: int, detail, violations: list | None = None):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail
        self.violations = violations or []


def _intent_dict(intent: TradeIntent) -> dict:
    # Safeguard clamp before risk validation — guarantees no path (manual approve or auto)
    # ever sizes a trade above MAX_POSITION_PCT, even for an intent created earlier.
    size = clamp_position_size(intent.position_size_percent, float(settings.max_position_pct))
    return {
        "ticker": intent.ticker,
        "action": intent.action.value,
        "order_type": intent.order_type,
        "limit_price": float(intent.limit_price) if intent.limit_price else 0,
        "position_size_percent": size,
        "stop_loss_percent": float(intent.stop_loss_percent or 0),
        "take_profit_percent": float(intent.take_profit_percent or 0),
    }


def intent_age_minutes(intent: TradeIntent, now: datetime | None = None) -> float | None:
    if intent.created_at is None:
        return None
    created = intent.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - created).total_seconds() / 60


def expire_if_stale(db: Session, intent: TradeIntent) -> None:
    """Cancel and refuse an intent older than INTENT_MAX_AGE_MIN (raises ExecutionError)."""
    age = intent_age_minutes(intent)
    if age is None or age <= settings.intent_max_age_min:
        return
    intent.status = IntentStatus.canceled
    audit(db, "intent_expired", "system", intent_id=intent.id,
          payload={"age_minutes": round(age, 1), "max_age_minutes": settings.intent_max_age_min})
    log_event(logger, "intent.expired", logging.WARNING, intent_id=intent.id, ticker=intent.ticker,
              age_minutes=round(age, 1), max_age_minutes=settings.intent_max_age_min)
    raise ExecutionError(409, f"Intent expired: it is {age:.0f} minutes old (max "
                              f"{settings.intent_max_age_min}). Re-run the analysis for a fresh proposal.")


def _block_entry(db: Session, intent: TradeIntent, actor: str, auto: bool, status_code: int,
                 code: str, detail: str, extra: dict | None = None) -> None:
    """Fail closed on a new entry: audit + log, leave the intent pending, raise."""
    audit(db, "entry_blocked", actor, intent_id=intent.id,
          payload={"ticker": intent.ticker, "auto": auto, "code": code, "reason": detail, **(extra or {})})
    log_event(logger, "order.entry_blocked", logging.WARNING, intent_id=intent.id, ticker=intent.ticker,
              actor=actor, auto=auto, code=code, reason=detail)
    raise ExecutionError(status_code, f"New entry blocked ({code}): {detail}")


def check_entry_data(db: Session, intent: TradeIntent, intent_dict: dict, actor: str, auto: bool) -> None:
    """BUY only. Fetch a FRESH quote right before submitting and refuse the entry when the
    quote is missing/stale, or the limit price is off-market. Any failure to get the data
    refuses too (fail closed). Sells and protective exits never come through here."""
    ticker = intent.ticker
    try:
        quote = market_data.fetch_snapshots([ticker]).get(ticker)
        clock = market_data.market_clock()
    except Exception as e:  # noqa: BLE001
        _block_entry(db, intent, actor, auto, 503, "MARKET_DATA_UNAVAILABLE",
                     f"could not fetch a fresh quote for {ticker}: {e}")
    if not quote:
        _block_entry(db, intent, actor, auto, 503, "NO_QUOTE", f"no fresh quote for {ticker}")
    flags = snapshots.quality_flags(quote, market_open=clock.get("is_open"),
                                    now=datetime.now(timezone.utc),
                                    max_quote_age_sec=settings.max_quote_age_sec)
    last_price = quote.get("last_price")
    if not last_price:
        _block_entry(db, intent, actor, auto, 409, "NO_MARKET_PRICE", f"no last price for {ticker}")
    if flags["stale"]:
        _block_entry(db, intent, actor, auto, 409, "STALE_QUOTE",
                     f"quote for {ticker} is stale ({flags['quote_age_sec']}s old, max "
                     f"{settings.max_quote_age_sec}s)", {"quote_age_sec": flags["quote_age_sec"]})
    off = intent_validation.check_limit_price(intent_dict["limit_price"], last_price,
                                              float(settings.max_limit_deviation_pct))
    if off:
        _block_entry(db, intent, actor, auto, 409, off[0]["code"], off[0]["message"],
                     {"last_price": last_price, "limit_price": intent_dict["limit_price"]})


def _lock_intent(db, intent) -> None:
    """SELECT ... FOR UPDATE on the intent row (no-op for the non-SQLAlchemy fakes in tests)."""
    if isinstance(db, Session):
        db.refresh(intent, with_for_update=True)


def execute_intent(db: Session, intent: TradeIntent, actor: str, *, auto: bool = False) -> Order:
    """Re-validate risk and submit a bracket order. Caller commits afterward."""
    # Serialize every path that can execute this intent (manual approve, analysis
    # auto-execute, monitor auto-buy) on the intent row, and re-check its status under the
    # lock: a second caller waits, then sees it is no longer pending.
    _lock_intent(db, intent)
    if intent.status != IntentStatus.pending_approval:
        raise ExecutionError(409, f"Intent is {intent.status.value}, not awaiting execution")

    # 0. Fail closed on stale proposals: an intent is only valid for a short window after
    #    the market snapshot it was based on.
    expire_if_stale(db, intent)

    # 0b. Automation gates (defense in depth — automated callers check the gate too):
    #     an automated order needs mode == auto + env capability + kill switch off, and the
    #     kill switch blocks new buys even from manual approval. The intent stays pending.
    gate = automation.order_gate(db)
    blocked = None
    if auto and not gate.allowed:
        blocked = f"Automated order blocked: {gate.reason}"
    elif gate.kill_switch and intent.action.value == "buy":
        blocked = f"New buys are blocked: {gate.reason}"
    if blocked:
        audit(db, "auto_order_blocked", actor, intent_id=intent.id,
              payload={"ticker": intent.ticker, "action": intent.action.value, "auto": auto,
                       "mode": gate.mode, "kill_switch": gate.kill_switch, "reason": gate.reason})
        log_event(logger, "order.blocked", logging.WARNING, intent_id=intent.id, ticker=intent.ticker,
                  action=intent.action.value, actor=actor, auto=auto, reason=gate.reason)
        raise ExecutionError(423, blocked)

    # 1. Fresh account/positions (state may have moved since the recommendation)
    try:
        account = broker.get_account()
        positions = broker.get_positions()
    except Exception as e:  # noqa: BLE001
        if intent.action.value == "buy":  # fail closed on new entries, with an audit trail
            _block_entry(db, intent, actor, auto, 502, "BROKER_UNAVAILABLE", f"Broker error: {e}")
        raise ExecutionError(502, f"Broker error: {e}")
    account["open_positions"] = len(positions)
    held_symbols = {p["symbol"] for p in positions}

    halted, _reason = risk_state.is_halted(db, account)
    intent_dict = _intent_dict(intent)

    # 2. RE-VALIDATE risk (defense in depth — identical for manual and auto)
    ri = build_risk_input(intent_dict, account, held_symbols, halted)
    result = risk.evaluate(ri)
    if not result.passed:
        codes = [v["code"] for v in result.as_violation_dicts()]
        log_event(logger, "risk.rejected", logging.WARNING, intent_id=intent.id, ticker=intent.ticker,
                  action=intent.action.value, actor=actor, auto=auto, risk_passed=False,
                  violations=codes, open_positions=len(positions), held=sorted(held_symbols),
                  max_open_positions=risk.settings.max_open_positions)
        intent.status = IntentStatus.rejected_by_risk
        intent.risk_passed = False
        intent.risk_violations = result.as_violation_dicts()
        audit(db, "risk_rejection", "risk_engine", intent_id=intent.id,
              payload={"violations": result.as_violation_dicts(),
                       "at": "auto" if auto else "approval",
                       "filled_positions": len(positions),
                       "held_symbols": sorted(held_symbols)})
        raise ExecutionError(422, {"violations": result.as_violation_dicts()},
                             violations=result.as_violation_dicts())

    # 2'. BUY only: adaptive risk policies on FRESH positions/account. They can only shrink
    #     the size (qty is recomputed by risk.evaluate) or reject; any failure to evaluate
    #     them blocks the entry (fail closed).
    if intent.action.value == "buy":
        try:
            policy_symbols = list(dict.fromkeys([intent.ticker] + [p["symbol"] for p in positions]))
            result, policy_notes = risk_policies.apply(
                intent_dict, ri, result, positions=positions,
                market=risk_policy_data.market_from_bars(db, intent.ticker, intent_dict["limit_price"]),
                sector_of=risk_policy_data.sector_of(db),
                returns_by_symbol=risk_policy_data.returns_by_symbol(db, policy_symbols))
        except Exception as e:  # noqa: BLE001
            _block_entry(db, intent, actor, auto, 503, "RISK_POLICY_ERROR",
                         f"risk policies could not be evaluated: {safe_error(e)}")
        if not result.passed:
            violations = result.as_violation_dicts()
            intent.status = IntentStatus.rejected_by_risk
            intent.risk_passed = False
            intent.risk_violations = violations
            audit(db, "risk_rejection", "risk_engine", intent_id=intent.id,
                  payload={"violations": violations, "at": "auto" if auto else "approval",
                           "stage": "risk_policies"})
            log_event(logger, "risk.rejected", logging.WARNING, intent_id=intent.id, ticker=intent.ticker,
                      actor=actor, auto=auto, risk_passed=False, violations=[v["code"] for v in violations])
            raise ExecutionError(422, {"violations": violations}, violations=violations)
        new_size = float(intent_dict["position_size_percent"])
        if new_size < float(intent.position_size_percent or 0):
            audit(db, "risk_policy_adjusted", "risk_engine", intent_id=intent.id,
                  payload={"from": float(intent.position_size_percent or 0), "to": new_size,
                           "notes": policy_notes})
            intent.position_size_percent = new_size

    intent.computed_qty = result.computed_qty
    intent.computed_notional = (
        float(result.computed_notional) if result.computed_notional else None
    )

    # 2a. BUY only: fresh quote + limit sanity check immediately before submission.
    if intent.action.value == "buy":
        check_entry_data(db, intent, intent_dict, actor, auto)

    # 2b. SELL reconciliation: NEVER trust the intended/db/recommended quantity. Query the
    #     broker for the actual holding and sell at most that. Partial fills make any other
    #     source unreliable (e.g. requested 13 but only 11 filled).
    if intent.action.value == "sell":
        requested_qty = result.computed_qty or 0
        held = broker.get_position_qty(intent.ticker)
        available = broker.get_available_qty(intent.ticker)  # held minus open-order reservations
        sell_qty = broker.reconcile_sell_qty(requested_qty, available)
        logger.info(
            "SELL reconcile: ticker=%s held=%s available_qty=%s database_qty=%s "
            "requested_qty=%s -> sell_qty=%s",
            intent.ticker, held, available, intent.computed_qty, requested_qty, sell_qty,
        )
        # Whole position already covered by open stop/take orders -> skip (not a failure).
        if held > 0 and available <= 0:
            intent.status = IntentStatus.canceled
            audit(db, "POSITION_ALREADY_PROTECTED", "system", intent_id=intent.id,
                  payload={"ticker": intent.ticker, "held": held, "available": 0,
                           "reason": "all shares reserved by open stop/take orders"})
            logger.info("POSITION_ALREADY_PROTECTED ticker=%s held=%s — sell skipped",
                        intent.ticker, held)
            raise ExecutionError(409, f"POSITION_ALREADY_PROTECTED: {intent.ticker} is already "
                                      "covered by open stop/take orders")
        if sell_qty <= 0:
            intent.status = IntentStatus.order_failed
            audit(db, "order_error", "system", intent_id=intent.id,
                  payload={"error": "no shares available to sell", "ticker": intent.ticker,
                           "held": held, "available": available, "requested_qty": requested_qty})
            raise ExecutionError(422, f"No shares available to sell for {intent.ticker} "
                                      f"(held={held}, available={available})")
        result.computed_qty = sell_qty
        intent.computed_qty = sell_qty
        # Keep the recorded notional consistent with the (possibly clamped) sell qty.
        intent.computed_notional = round(sell_qty * float(intent.limit_price or 0), 2)

    # 3. Submit a bracket order. The client_order_id is DETERMINISTIC per intent, so a
    #    duplicate approve/auto request can never place a second order: the orders table
    #    (unique client_order_id) and the broker both reject the repeat.
    created = intent.created_at.timestamp() if intent.created_at else 0
    client_order_id = f"intent-{intent.id}-{int(created)}"
    if db.scalars(select(Order.id).where(Order.client_order_id == client_order_id)).first() is not None:
        audit(db, "duplicate_submit_blocked", actor, intent_id=intent.id,
              payload={"client_order_id": client_order_id, "ticker": intent.ticker})
        raise ExecutionError(409, f"Intent {intent.id} already has an order; duplicate submit blocked")
    try:
        order_data = broker.submit_bracket_order(intent_dict, result.computed_qty, client_order_id)
    except broker.PositionAlreadyProtected as e:
        intent.status = IntentStatus.canceled
        audit(db, "POSITION_ALREADY_PROTECTED", "system", intent_id=intent.id,
              payload={"ticker": intent.ticker, "reason": str(e)})
        raise ExecutionError(409, str(e))
    except Exception as e:  # noqa: BLE001
        # The error may hide an order the broker DID accept (e.g. a timeout on the response).
        # Look it up by our deterministic client_order_id before declaring failure.
        accepted = broker.find_order_by_client_id(client_order_id)
        if accepted is None or not accepted.get("broker_order_id"):
            intent.status = IntentStatus.order_failed
            audit(db, "order_error", "system", intent_id=intent.id, payload={"error": str(e)})
            raise ExecutionError(502, f"Broker rejected order: {e}")
        entry = float(intent_dict.get("limit_price") or 0)
        stop_pct = float(intent_dict.get("stop_loss_percent") or 0)
        order_data = {
            "broker_order_id": accepted["broker_order_id"],
            "symbol": accepted.get("symbol") or intent.ticker,
            "side": accepted.get("side") or intent.action.value,
            "qty": accepted.get("qty") or result.computed_qty,
            "order_type": accepted.get("order_type") or "limit",
            "limit_price": accepted.get("limit_price") or entry,
            "stop_loss_price": round(entry * (1 - stop_pct / 100), 2)
            if intent.action.value == "buy" and entry and stop_pct else None,
            "take_profit_price": None, "status": accepted.get("status") or "accepted",
            "raw": {"recovered_after_error": safe_error(e, 300), "broker": accepted},
        }
        audit(db, "order_submit_recovered", "system", intent_id=intent.id,
              payload={"client_order_id": client_order_id,
                       "broker_order_id": accepted["broker_order_id"], "error": safe_error(e, 300)})
        log_event(logger, "order.submit_recovered", logging.WARNING, intent_id=intent.id,
                  broker_order_id=accepted["broker_order_id"], client_order_id=client_order_id)

    order = Order(
        intent_id=intent.id,
        broker_order_id=order_data["broker_order_id"],
        client_order_id=client_order_id,
        symbol=order_data["symbol"],
        side=order_data["side"],
        qty=order_data["qty"],
        order_type=order_data["order_type"],
        limit_price=order_data["limit_price"],
        stop_loss_price=order_data["stop_loss_price"],
        take_profit_price=order_data["take_profit_price"],
        status=order_data["status"],
        is_live=not account["is_paper"],
        monitoring_status="active",
        raw=order_data["raw"],
    )
    db.add(order)
    db.flush()

    intent.status = IntentStatus.submitted
    intent.approved_by = actor
    intent.approved_at = datetime.now(timezone.utc)
    intent.is_live = not account["is_paper"]
    intent.auto_executed = auto

    if auto:
        event = "auto_buy_executed" if intent.action.value == "buy" else "auto_sell_executed"
        audit(db, event, actor, intent_id=intent.id, order_id=order.id,
              payload={"computed_qty": result.computed_qty, "is_live": intent.is_live,
                       "confidence": float(intent.confidence or 0)})
    else:
        audit(db, "approval", actor, intent_id=intent.id, order_id=order.id,
              payload={"computed_qty": result.computed_qty, "is_live": intent.is_live})

    audit(db, "order_submit", "system", intent_id=intent.id, order_id=order.id, payload=order_data)
    log_event(logger, "order.submitted", intent_id=intent.id, order_id=order.id,
              broker_order_id=order.broker_order_id, client_order_id=client_order_id,
              ticker=intent.ticker, side=order.side, qty=order.qty, limit_price=order.limit_price,
              stop_loss_price=order.stop_loss_price, actor=actor, auto=auto,
              is_live=order.is_live, risk_passed=True)
    return order
