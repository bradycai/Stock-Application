"""Journal records for exits the monitor sends through `broker.close_position`.

`close_position` places a closing order at the broker, but nothing in the database used to
describe it, so `order_sync` could never see the fill and the trade outcome stayed open.
`record_exit` writes the missing lineage:

  * a `trade_intents` row (action sell, strategy "monitor-exit", status submitted) that
    names the exit reason and the entry intent it closes, and
  * an `orders` row carrying the broker order id, the closing quantity and `exit_reason`.

An intent row is used instead of making `orders.intent_id` nullable: every order keeps a
parent intent (the audit trail and the dashboards already join on it) and no migration is
needed. The records only describe an order that WAS accepted by the broker (it has an order
id); they never place or change anything. `order_sync` then tracks the fill and closes the
trade outcome from real broker state.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import audit
from app.logging_config import log_event
from app.models import AnalysisRun, IntentAction, IntentStatus, Order, TradeIntent

logger = logging.getLogger("trading.exits")

EXIT_STRATEGY_ID = "monitor-exit"
# Exit reasons that only trim a position: their fills must not close the trade outcome.
PARTIAL_EXIT_REASONS = ("claude_trim",)


def is_partial_exit(reason: str | None) -> bool:
    r = reason or ""
    return r.startswith("partial_") or r in PARTIAL_EXIT_REASONS


def record_exit(db: Session, *, symbol: str, result: dict | None, reason: str, qty: int | float | None,
                is_live: bool, price: float | None = None, actor: str = "monitor") -> Order | None:
    """Record a sent exit order. Returns the Order, or None when the broker returned no order
    id (nothing was placed, e.g. no position). Idempotent per broker order id."""
    result = result if isinstance(result, dict) else {}
    broker_order_id = str(result.get("broker_order_id") or "")
    if not broker_order_id:
        return None
    existing = db.scalars(select(Order).where(Order.broker_order_id == broker_order_id)).first()
    if existing is not None:
        return existing

    exit_qty = int(float(result.get("qty") or qty or 0))
    entry_intent = db.scalars(
        select(TradeIntent).where(TradeIntent.ticker == symbol, TradeIntent.action == IntentAction.buy)
        .order_by(TradeIntent.created_at.desc(), TradeIntent.id.desc())
    ).first()
    if entry_intent is not None:
        run_id = entry_intent.run_id
    else:
        run = AnalysisRun(model="monitor-exit", prompt_tokens=0, output_tokens=0,
                          input_context={"source": "monitor_exit"}, raw_response={})
        db.add(run)
        db.flush()
        run_id = run.id

    now = datetime.now(timezone.utc)
    evidence = [f"exit_reason: {reason}"]
    if entry_intent is not None:
        evidence.append(f"closes_entry_intent: {entry_intent.id}")
    intent = TradeIntent(
        run_id=run_id, ticker=symbol, action=IntentAction.sell, order_type="market",
        limit_price=price, computed_qty=exit_qty or None, reason=f"monitor exit: {reason}",
        status=IntentStatus.submitted, strategy_id=EXIT_STRATEGY_ID, evidence=evidence,
        approved_by=actor, approved_at=now, is_live=is_live, auto_executed=True,
    )
    db.add(intent)
    db.flush()
    order = Order(
        intent_id=intent.id, broker_order_id=broker_order_id,
        client_order_id=str(result.get("client_order_id") or f"exit-{broker_order_id}"),
        symbol=symbol, side="sell", qty=exit_qty, order_type="market",
        limit_price=None, status=str(result.get("status") or "submitted").lower().split(".")[-1],
        is_live=is_live, exit_reason=reason,
        raw={"source": "monitor_exit", "reason": reason, "broker": result},
    )
    db.add(order)
    db.flush()
    audit(db, "exit_order_recorded", actor, intent_id=intent.id, order_id=order.id,
          payload={"symbol": symbol, "reason": reason, "qty": exit_qty,
                   "broker_order_id": broker_order_id,
                   "entry_intent_id": entry_intent.id if entry_intent is not None else None})
    log_event(logger, "exit.recorded", symbol=symbol, reason=reason, qty=exit_qty,
              broker_order_id=broker_order_id, order_id=order.id)
    return order
