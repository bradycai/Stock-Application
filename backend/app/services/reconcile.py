"""Read-only broker reconciliation: broker positions + open orders vs our database.

Runs from the monitor loop next to `order_sync`, in every automation mode. The broker is the
source of truth and this module only READS from it (positions, open orders, one order's
details); it never places, changes or cancels an order. A requested sell is never assumed
to have completed: a position row is closed only because the broker no longer reports the
symbol as held.

  * `position_management` rows for symbols the broker no longer holds are closed (with the
    reason recorded in `last_decision` and an audit row).
  * Broker positions the database knows nothing about (no order, no managed row) are
    flagged with an audit row and a warning log (deduplicated for 24 hours).
  * Our non-terminal orders that the broker does not list as open are checked: an order the
    broker has no record of (404) is marked `missing_at_broker`; one whose state simply
    lags is left for `order_sync` to journal (so fills and outcomes are never skipped).
  * A recorded exit order that ended unfilled (canceled/rejected/expired) while the position
    is still held is flagged once, so the operator sees that the exit did not happen.

If the broker cannot be read, nothing is changed.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import audit
from app.logging_config import log_event, safe_error
from app.models import AuditLog, Order, PositionManagement
from app.services import broker
from app.services.order_sync import TERMINAL_STATUSES, _norm_status

logger = logging.getLogger("trading.reconcile")

UNKNOWN_POSITION_DEDUPE = timedelta(hours=24)
EXIT_LOOKBACK = timedelta(days=2)
# A position or order must be reported missing by two passes at least this far apart before
# the database is changed, so one empty or glitchy broker response can't close every managed
# position (losing trailing-stop / partial-profit state) or end tracking of a live order.
CONFIRM_MISSING_AFTER = timedelta(seconds=50)
LAG_AUDIT_DEDUPE = timedelta(hours=1)
_missing_since: dict[tuple[str, str], datetime] = {}


def _confirmed_missing(key: tuple[str, str], now: datetime) -> bool:
    """True once `key` has been missing on an earlier pass at least CONFIRM_MISSING_AFTER ago."""
    first = _missing_since.setdefault(key, now)
    return now - first >= CONFIRM_MISSING_AFTER


def _seen(key: tuple[str, str]) -> None:
    _missing_since.pop(key, None)
FAILED_EXIT_STATUSES = {"canceled", "expired", "rejected"}


def _recently_audited(db: Session, event_type: str, *, symbol: str | None = None,
                      order_id: int | None = None, within: timedelta | None = None) -> bool:
    stmt = select(AuditLog.id).where(AuditLog.event_type == event_type)
    if symbol is not None:
        stmt = stmt.where(AuditLog.payload["symbol"].astext == symbol)
    if order_id is not None:
        stmt = stmt.where(AuditLog.order_id == order_id)
    if within is not None:
        stmt = stmt.where(AuditLog.ts > datetime.now(timezone.utc) - within)
    return db.execute(stmt.limit(1)).first() is not None


def reconcile(db: Session) -> dict:
    """One reconciliation pass. Never raises on broker errors; returns a counts dict."""
    counts = {"positions_closed": 0, "unknown_positions": 0, "orders_missing": 0,
              "orders_lagging": 0, "failed_exits": 0, "error": None}
    try:
        positions = broker.get_positions()
        open_orders = broker.list_open_orders()
    except Exception as e:  # noqa: BLE001
        counts["error"] = safe_error(e, 300)
        log_event(logger, "reconcile.broker_unavailable", logging.WARNING, error=counts["error"])
        return counts

    held = {p["symbol"] for p in positions}
    open_ids = {o["broker_order_id"] for o in open_orders}

    # 1. Managed positions the broker no longer holds.
    managed = db.scalars(select(PositionManagement).where(PositionManagement.is_open.is_(True))).all()
    now = datetime.now(timezone.utc)
    for pm in managed:
        if pm.symbol in held:
            _seen(("position", pm.symbol))
            continue
        if not _confirmed_missing(("position", pm.symbol), now):
            continue  # wait for a second pass before trusting "not held"
        _seen(("position", pm.symbol))
        pm.is_open = False
        pm.closed_at = now
        pm.last_decision = "reconciled_not_held_at_broker"
        audit(db, "reconcile_position_closed", "reconcile",
              payload={"symbol": pm.symbol, "position_management_id": pm.id,
                       "reason": "broker reports no position"})
        log_event(logger, "reconcile.position_closed", symbol=pm.symbol, position_management_id=pm.id)
        counts["positions_closed"] += 1

    # 2. Broker positions the database does not know about.
    managed_open = {pm.symbol for pm in managed if pm.is_open}
    for sym in sorted(held):
        if sym in managed_open:
            continue
        known = db.execute(select(Order.id).where(Order.symbol == sym).limit(1)).first() is not None
        if known or _recently_audited(db, "reconcile_unknown_position", symbol=sym,
                                      within=UNKNOWN_POSITION_DEDUPE):
            continue
        qty = next((p["qty"] for p in positions if p["symbol"] == sym), None)
        audit(db, "reconcile_unknown_position", "reconcile",
              payload={"symbol": sym, "qty": qty,
                       "note": "held at the broker but no order or managed record exists"})
        log_event(logger, "reconcile.unknown_position", logging.WARNING, symbol=sym, qty=qty)
        counts["unknown_positions"] += 1

    # 3. Our non-terminal orders the broker does not list as open.
    pending = [o for o in db.scalars(select(Order).where(Order.broker_order_id.is_not(None))).all()
               if _norm_status(o.status) not in TERMINAL_STATUSES and o.broker_order_id not in open_ids]
    for order in pending:
        try:
            details = broker.get_order_details(order.broker_order_id)
        except Exception as e:  # noqa: BLE001
            if getattr(e, "status_code", None) == 404:
                if not _confirmed_missing(("order", str(order.id)), now):
                    continue  # a single 404 may be transient: confirm on a later pass
                _seen(("order", str(order.id)))
                order.status = "missing_at_broker"
                audit(db, "reconcile_order_missing", "reconcile", order_id=order.id,
                      payload={"symbol": order.symbol, "broker_order_id": order.broker_order_id,
                               "was": "open in database, unknown to the broker"})
                log_event(logger, "reconcile.order_missing", logging.WARNING, order_id=order.id,
                          broker_order_id=order.broker_order_id)
                counts["orders_missing"] += 1
            else:
                log_event(logger, "reconcile.order_lookup_failed", logging.WARNING, order_id=order.id,
                          error=safe_error(e, 300))
            continue
        # The broker no longer has it open but our row is still live: leave the status
        # change (and any fill/outcome journaling) to order_sync's next pass.
        _seen(("order", str(order.id)))
        if _recently_audited(db, "reconcile_order_lag", order_id=order.id, within=LAG_AUDIT_DEDUPE):
            counts["orders_lagging"] += 1
            continue
        audit(db, "reconcile_order_lag", "reconcile", order_id=order.id,
              payload={"symbol": order.symbol, "db_status": order.status,
                       "broker_status": _norm_status(details.get("status"))})
        counts["orders_lagging"] += 1

    # 4. Exit orders that ended without a fill while the position is still held.
    recent_exits = db.scalars(
        select(Order).where(Order.side == "sell", Order.exit_reason.is_not(None),
                            Order.submitted_at > now - EXIT_LOOKBACK)).all()
    for order in recent_exits:
        if (_norm_status(order.status) in FAILED_EXIT_STATUSES and not (order.filled_qty or 0)
                and order.symbol in held
                and not _recently_audited(db, "reconcile_exit_not_filled", order_id=order.id)):
            audit(db, "reconcile_exit_not_filled", "reconcile", order_id=order.id,
                  payload={"symbol": order.symbol, "exit_reason": order.exit_reason,
                           "status": order.status, "note": "position is still held at the broker"})
            log_event(logger, "reconcile.exit_not_filled", logging.WARNING, symbol=order.symbol,
                      order_id=order.id, status=order.status, reason=order.exit_reason)
            counts["failed_exits"] += 1

    db.commit()
    log_event(logger, "reconcile.done", **counts)
    return counts
