"""Read-only order sync for the trade journal.

Polls the broker for orders that are not yet terminal (plus filled buys whose bracket exit
may still be pending), updates our `orders` rows, appends an `order_fills` row whenever an
order's state changes, and maintains `trade_outcomes`: a buy fill opens one, a sell fill
(or a filled bracket exit leg) closes it with realized P&L and the SPY return over the
same period. It only READS from the broker; it never places, modifies or cancels orders,
so it is safe to run in every automation mode.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_config import log_event, safe_error
from app.models import Order, OrderFill, TradeOutcome
from app.services import broker, outcomes

logger = logging.getLogger("trading.order_sync")

TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced"}


def _norm_status(status) -> str:
    s = str(getattr(status, "value", status) or "").lower()
    return s.split(".")[-1]


def _aware(ts: datetime | None) -> datetime | None:
    if ts is not None and ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _open_outcome(db: Session, symbol: str) -> TradeOutcome | None:
    return db.execute(
        select(TradeOutcome).where(TradeOutcome.symbol == symbol, TradeOutcome.status == "open")
        .order_by(TradeOutcome.entry_ts, TradeOutcome.id).limit(1)
    ).scalar_one_or_none()


def _record_fill(db: Session, order: Order, details: dict, status: str) -> bool:
    """Append an order_fills row unless the latest one already has this exact state."""
    price = details.get("filled_avg_price")
    filled_qty = int(details.get("filled_qty") or 0)
    last = db.execute(
        select(OrderFill).where(OrderFill.order_id == order.id).order_by(OrderFill.id.desc()).limit(1)
    ).scalar_one_or_none()
    if last is not None and last.status == status and last.filled_qty == filled_qty \
            and (float(last.filled_avg_price) if last.filled_avg_price is not None else None) == price:
        return False
    db.add(OrderFill(
        order_id=order.id, broker_order_id=order.broker_order_id, status=status,
        requested_qty=order.qty, filled_qty=filled_qty, filled_avg_price=price,
        filled_at=_aware(details.get("filled_at")),
        raw={k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in details.items()},
    ))
    return True


def _close_outcome(db: Session, outcome: TradeOutcome, *, exit_price: float, exit_ts: datetime,
                   exit_order_id: int | None, reason: str) -> None:
    entry = float(outcome.entry_price) if outcome.entry_price is not None else None
    outcome.exit_price = exit_price
    outcome.exit_ts = exit_ts
    outcome.exit_order_id = exit_order_id
    outcome.exit_reason = reason
    outcome.status = "closed"
    outcome.computed_at = datetime.now(timezone.utc)
    if entry:
        outcome.realized_pnl = round((exit_price - entry) * outcome.qty, 2)
        outcome.realized_pnl_pct = outcomes.return_pct(entry, exit_price)
    if outcome.entry_ts:
        entry_ts = _aware(outcome.entry_ts)
        outcome.holding_days = round((exit_ts - entry_ts).total_seconds() / 86400, 4)
        outcome.benchmark_return_pct = outcomes.benchmark_return(db, entry_ts, exit_ts)


def _exit_reason_for_leg(leg: dict) -> str:
    otype = str(leg.get("order_type") or "").lower()
    if "stop" in otype:
        return "stop_loss"
    if otype == "limit":
        return "take_profit"
    return "bracket_exit"


def _apply_outcomes(db: Session, order: Order, details: dict, status: str) -> None:
    now = datetime.now(timezone.utc)
    filled_ts = _aware(details.get("filled_at")) or now
    price = details.get("filled_avg_price")

    if order.side == "buy" and status == "filled" and price:
        exists = db.execute(select(TradeOutcome.id).where(TradeOutcome.entry_order_id == order.id)
                            ).scalar_one_or_none()
        if exists is None:
            db.add(TradeOutcome(
                intent_id=order.intent_id, symbol=order.symbol, entry_order_id=order.id,
                entry_ts=filled_ts, entry_price=price,
                qty=int(details.get("filled_qty") or order.qty),
                benchmark_symbol=outcomes.BENCHMARK, status="open",
            ))
            db.flush()
        # A bracket exit leg may have filled at the broker.
        outcome = db.execute(select(TradeOutcome).where(TradeOutcome.entry_order_id == order.id)
                             ).scalar_one()
        if outcome.status == "open":
            for leg in details.get("legs") or []:
                if _norm_status(leg.get("status")) == "filled" and leg.get("side") == "sell" \
                        and leg.get("filled_avg_price"):
                    _close_outcome(db, outcome, exit_price=leg["filled_avg_price"],
                                   exit_ts=_aware(leg.get("filled_at")) or now, exit_order_id=None,
                                   reason=_exit_reason_for_leg(leg))
                    break

    elif order.side == "sell" and status == "filled" and price:
        outcome = _open_outcome(db, order.symbol)
        if outcome is not None and (outcome.entry_ts is None or filled_ts >= _aware(outcome.entry_ts)):
            _close_outcome(db, outcome, exit_price=price, exit_ts=filled_ts, exit_order_id=order.id,
                           reason=order.exit_reason or "sell_order")


def _orders_to_sync(db: Session) -> list[Order]:
    open_entries = select(TradeOutcome.entry_order_id).where(
        TradeOutcome.status == "open", TradeOutcome.entry_order_id.is_not(None))
    orders = db.execute(
        select(Order).where(Order.broker_order_id.is_not(None)).order_by(Order.id)).scalars().all()
    open_ids = set(db.execute(open_entries).scalars().all())
    return [o for o in orders
            if _norm_status(o.status) not in TERMINAL_STATUSES or o.id in open_ids]


def sync_orders(db: Session) -> dict:
    """Refresh non-terminal orders from the broker (read-only) and update the journal.
    A broker error on one order is logged and skipped; it never raises."""
    counts = {"checked": 0, "updated": 0, "fills_recorded": 0, "errors": 0}
    for order in _orders_to_sync(db):
        counts["checked"] += 1
        try:
            details = broker.get_order_details(order.broker_order_id)
            status = _norm_status(details.get("status"))
            changed = (status != _norm_status(order.status)
                       or int(details.get("filled_qty") or 0) != (order.filled_qty or 0))
            order.status = status
            order.filled_qty = int(details.get("filled_qty") or 0)
            if details.get("filled_avg_price") is not None:
                order.filled_avg_price = details["filled_avg_price"]
            if _record_fill(db, order, details, status):
                counts["fills_recorded"] += 1
            _apply_outcomes(db, order, details, status)
            db.commit()
            counts["updated"] += int(changed)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            counts["errors"] += 1
            log_event(logger, "order_sync.failed", logging.WARNING, order_id=order.id,
                      error=safe_error(e, 300))
    log_event(logger, "order_sync.done", **counts)
    return counts
