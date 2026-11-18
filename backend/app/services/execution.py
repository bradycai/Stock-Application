"""Shared intent -> order execution.

Both the manual approval endpoint and the autonomous path call execute_intent(). It
re-validates risk against FRESH state, submits a bracket order, records the order, and
writes audit rows. It does NOT commit — the caller owns the transaction so it can decide
how to surface success/failure.

Keeping this in one place means manual approval and auto-trading share the exact same
risk re-validation and bracket-order submission. Manual approval behavior is unchanged.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.deps import audit
from app.logging_config import log_event
from app.models import IntentStatus, Order, TradeIntent
from app.services import broker, risk, risk_state
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


def execute_intent(db: Session, intent: TradeIntent, actor: str, *, auto: bool = False) -> Order:
    """Re-validate risk and submit a bracket order. Caller commits afterward."""
    # 1. Fresh account/positions (state may have moved since the recommendation)
    try:
        account = broker.get_account()
        positions = broker.get_positions()
    except Exception as e:  # noqa: BLE001
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

    intent.computed_qty = result.computed_qty
    intent.computed_notional = (
        float(result.computed_notional) if result.computed_notional else None
    )

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

    # 3. Submit a bracket order (idempotent via client_order_id)
    client_order_id = f"intent-{intent.id}-{uuid.uuid4().hex[:8]}"
    try:
        order_data = broker.submit_bracket_order(intent_dict, result.computed_qty, client_order_id)
    except broker.PositionAlreadyProtected as e:
        intent.status = IntentStatus.canceled
        audit(db, "POSITION_ALREADY_PROTECTED", "system", intent_id=intent.id,
              payload={"ticker": intent.ticker, "reason": str(e)})
        raise ExecutionError(409, str(e))
    except Exception as e:  # noqa: BLE001
        intent.status = IntentStatus.order_failed
        audit(db, "order_error", "system", intent_id=intent.id, payload={"error": str(e)})
        raise ExecutionError(502, f"Broker rejected order: {e}")

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
