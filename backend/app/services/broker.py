"""Alpaca execution. Paper by default; live only when TRADING_MODE=live.

This is the ONLY module that holds broker credentials and the ONLY one that can place
an order. Claude has no access to any of it.
"""
import logging
from datetime import datetime, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    StopLossRequest,
    StopOrderRequest,
)

from app.config import settings

logger = logging.getLogger("trading.broker")

_trading: TradingClient | None = None
_data: StockHistoricalDataClient | None = None


def trading_client() -> TradingClient:
    global _trading
    if _trading is None:
        _trading = TradingClient(
            settings.alpaca_key,
            settings.alpaca_secret,
            paper=not settings.is_live,
        )
    return _trading


def data_client() -> StockHistoricalDataClient:
    global _data
    if _data is None:
        _data = StockHistoricalDataClient(settings.alpaca_key, settings.alpaca_secret)
    return _data


# --------------------------------------------------------------------------- #
# Account / positions                                                         #
# --------------------------------------------------------------------------- #
def get_account() -> dict:
    acct = trading_client().get_account()
    equity = float(acct.equity)
    last_equity = float(acct.last_equity) if acct.last_equity else equity
    day_pnl = equity - last_equity
    day_pnl_pct = (day_pnl / last_equity * 100) if last_equity else 0.0
    return {
        "equity": equity,
        "cash": float(acct.cash),
        "buying_power": float(acct.buying_power),
        "last_equity": last_equity,
        "day_pnl": day_pnl,
        "day_pnl_pct": day_pnl_pct,
        "is_paper": not settings.is_live,
        "is_margin": acct.multiplier is not None and float(acct.multiplier) > 1,
        "raw": {
            "account_number": acct.account_number,
            "status": str(acct.status),
            "multiplier": acct.multiplier,
            "equity": str(acct.equity),
            "cash": str(acct.cash),
        },
    }


def get_positions() -> list[dict]:
    positions = trading_client().get_all_positions()
    out = []
    for p in positions:
        out.append(
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price or 0),
                "market_value": float(p.market_value or 0),
                "unrealized_pl": float(p.unrealized_pl or 0),
                "unrealized_plpc": float(p.unrealized_plpc or 0) * 100,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Market data                                                                 #
# --------------------------------------------------------------------------- #
def get_quote_and_intraday(symbol: str) -> dict:
    """Latest price plus the intraday % change vs today's open (for the parabolic-move
    risk rule). Falls back gracefully if data is unavailable."""
    client = data_client()
    last_price = 0.0
    try:
        q = client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))
        quote = q[symbol]
        last_price = float(quote.ask_price or quote.bid_price or 0)
    except Exception:
        pass

    intraday_change_pct = 0.0
    today_open = None
    volume = None
    try:
        bars = client.get_stock_bars(
            StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, limit=1)
        )
        if bars and symbol in bars.data and bars.data[symbol]:
            bar = bars.data[symbol][-1]
            today_open = float(bar.open)
            volume = int(bar.volume)
            if not last_price:
                last_price = float(bar.close)
            if today_open:
                intraday_change_pct = (last_price - today_open) / today_open * 100
    except Exception:
        pass

    return {
        "symbol": symbol,
        "last_price": last_price,
        "today_open": today_open,
        "volume": volume,
        "intraday_change_pct": round(intraday_change_pct, 4),
    }


# --------------------------------------------------------------------------- #
# Order execution — bracket (entry + stop + target, atomic)                   #
# --------------------------------------------------------------------------- #
class BracketValidationError(ValueError):
    """Raised when bracket prices are invalid — the order is NOT sent to the broker."""


class PositionAlreadyProtected(Exception):
    """All held shares are already reserved by open stop-loss / take-profit (bracket)
    orders, so there is nothing left to sell — the sell is skipped (not an error)."""


def validate_buy_bracket(entry: float, stop: float, take: float | None = None) -> tuple[bool, str]:
    """Validate a long entry's protective prices. The initial stop-loss must be below
    entry. Take-profit is OPTIONAL now (AI-managed exits replace the fixed take-profit);
    when provided it must be above entry. Pure → unit-tested. Returns (ok, reason)."""
    if entry <= 0:
        return False, f"entry price must be > 0 (got {entry})"
    if stop is None or stop <= 0:
        return False, f"stop_loss must be > 0 (got {stop})"
    if not (stop < entry):
        return False, f"stop_loss {stop} must be BELOW entry {entry}"
    if take is not None:
        if take <= 0 or not (entry < take):
            return False, f"take_profit {take} must be ABOVE entry {entry}"
    return True, "ok"


def submit_bracket_order(intent: dict, computed_qty: int, client_order_id: str) -> dict:
    """Submit an order to Alpaca.

    BUY  -> OTO (entry limit + an INITIAL protective stop-loss; NO fixed take-profit). The
            stop is validated below entry before submission. Take-profit exits are now
            AI-managed (trailing stop + partial profit-taking) rather than a fixed bracket
            target. Invalid prices raise BracketValidationError and are NOT sent.
    SELL -> a simple limit order to reduce/close a long (clamped to available qty).
    """
    ticker = intent["ticker"]
    entry = round(float(intent["limit_price"]), 2)
    stop_pct = float(intent["stop_loss_percent"])
    is_buy = intent["action"] == "buy"

    if is_buy:
        stop_price = round(entry * (1 - stop_pct / 100), 2)
        take_profit_price = None  # fixed take-profit removed — exits are AI-managed

        ok, reason = validate_buy_bracket(entry, stop_price, take_profit_price)
        if not ok:
            logger.error(
                "REJECTED entry (not sent): ticker=%s side=buy entry=%s stop=%s — %s",
                ticker, entry, stop_price, reason,
            )
            raise BracketValidationError(
                f"Invalid entry for BUY {ticker}: {reason} (entry={entry}, stop={stop_price})"
            )

        logger.info(
            "SUBMIT OTO entry: ticker=%s side=buy order_type=limit qty=%s entry=%s "
            "initial_stop=%s (no fixed take-profit; AI-managed exit)",
            ticker, computed_qty, entry, stop_price,
        )
        req = LimitOrderRequest(
            symbol=ticker, qty=computed_qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY, limit_price=entry,
            order_class=OrderClass.OTO,
            stop_loss=StopLossRequest(stop_price=stop_price),
            client_order_id=client_order_id,
        )
    else:
        # SELL: simple limit order to reduce/close — no bracket legs. Use the AVAILABLE
        # quantity (held minus shares reserved by open stop/take orders), never the
        # requested/db qty. If the whole position is already covered by open orders, skip.
        stop_price = None
        take_profit_price = None
        held = get_position_qty(ticker)
        available = get_available_qty(ticker)
        if held <= 0:
            raise BracketValidationError(
                f"No shares to sell for {ticker} (held=0, requested={computed_qty})"
            )
        if available <= 0:
            logger.info(
                "POSITION_ALREADY_PROTECTED: ticker=%s held=%s available=0 — all shares "
                "reserved by open stop/take orders; sell skipped (requested=%s)",
                ticker, held, computed_qty,
            )
            raise PositionAlreadyProtected(
                f"POSITION_ALREADY_PROTECTED: {ticker} all {held} share(s) already covered "
                "by open orders"
            )
        sell_qty = reconcile_sell_qty(computed_qty, available)
        logger.info(
            "SUBMIT order: ticker=%s side=sell order_type=limit requested_qty=%s held=%s "
            "available_qty=%s sell_qty=%s entry=%s (no bracket)",
            ticker, computed_qty, held, available, sell_qty, entry,
        )
        computed_qty = sell_qty
        req = LimitOrderRequest(
            symbol=ticker, qty=computed_qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY, limit_price=entry,
            client_order_id=client_order_id,
        )

    order = trading_client().submit_order(req)
    return {
        "broker_order_id": str(order.id),
        "client_order_id": order.client_order_id,
        "symbol": order.symbol,
        "side": str(order.side.value),
        "qty": int(float(order.qty)),
        "order_type": str(order.order_type.value),
        "limit_price": float(order.limit_price) if order.limit_price else entry,
        "stop_loss_price": stop_price,
        "take_profit_price": take_profit_price,
        "status": str(order.status.value),
        "submitted_at": (order.submitted_at or datetime.now(timezone.utc)).isoformat(),
        "raw": {"id": str(order.id), "status": str(order.status.value)},
    }


def cancel_open_orders(symbol: str) -> int:
    """Cancel all OPEN orders for a symbol (e.g. the protective OTO stop), freeing the
    reserved shares so the position can be sold/trimmed. Returns count canceled."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    orders = trading_client().get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    n = 0
    for o in orders:
        if o.symbol == symbol:
            try:
                trading_client().cancel_order_by_id(o.id)
                n += 1
            except Exception:  # noqa: BLE001
                pass
    if n:
        logger.info("Canceled %d open order(s) for %s", n, symbol)
    return n


def place_stop_order(symbol: str, qty: int, stop_price: float) -> dict:
    """Place a standalone protective STOP (sell) order for `qty` shares — used to re-arm a
    broker-side stop on the remaining shares after a partial sell."""
    if qty <= 0 or stop_price <= 0:
        return {"placed": False}
    order = trading_client().submit_order(
        StopOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC, stop_price=round(float(stop_price), 2),
        )
    )
    logger.info("Placed protective STOP for %s qty=%s stop=%s", symbol, qty, stop_price)
    return {"placed": True, "broker_order_id": str(getattr(order, "id", ""))}


def get_order_status(broker_order_id: str) -> dict:
    order = trading_client().get_order_by_id(broker_order_id)
    return {
        "status": str(order.status.value),
        "filled_qty": int(float(order.filled_qty or 0)),
        "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
    }


# --------------------------------------------------------------------------- #
# Position closing (used by the autonomous position monitor)                  #
# --------------------------------------------------------------------------- #
def get_latest_price(symbol: str) -> float:
    return get_quote_and_intraday(symbol)["last_price"]


def get_position_qty(symbol: str) -> int:
    """Broker-reported shares currently held for a symbol (0 if none). This is the ONLY
    trustworthy source of how many shares can be sold — never the intended/db/recommended
    quantity, which can diverge after a partial fill."""
    try:
        pos = trading_client().get_open_position(symbol)
        return int(float(pos.qty))
    except Exception:  # noqa: BLE001 - no position / not found
        return 0


def reconcile_sell_qty(requested: int, available: int) -> int:
    """Clamp a desired sell quantity to what is actually available. Pure → unit-tested.
    Never sells more than is held; returns 0 when nothing is available."""
    if available <= 0:
        return 0
    return min(max(0, int(requested)), int(available))


def get_open_sell_qty(symbol: str) -> int:
    """Shares of `symbol` currently reserved by OPEN sell orders (incl. bracket stop/take
    legs). These shares are held by the broker and cannot be sold again."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    orders = trading_client().get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    total = 0
    for o in orders:
        side = str(getattr(o.side, "value", o.side))
        if o.symbol == symbol and side == "sell":
            try:
                total += int(float(o.qty))
            except (TypeError, ValueError):
                pass
    return total


def get_available_qty(symbol: str) -> int:
    """Shares of `symbol` that can actually be sold = held minus shares reserved by open
    orders. Prefers Alpaca's server-computed `qty_available` (which already nets out open
    bracket legs); falls back to held - open-sell-orders."""
    try:
        pos = trading_client().get_open_position(symbol)
    except Exception:  # noqa: BLE001 - no position
        return 0
    avail = getattr(pos, "qty_available", None)
    if avail is not None:
        try:
            return max(0, int(float(avail)))
        except (TypeError, ValueError):
            pass
    held = int(float(pos.qty))
    return max(0, held - get_open_sell_qty(symbol))


def close_position(symbol: str, qty: int | None = None) -> dict:
    """Close a position. With `qty` it closes that many shares (a trim); without, it
    liquidates the whole position. Alpaca cancels the symbol's open orders and submits a
    closing order. Returns a small dict for auditing."""
    if qty is not None and qty > 0:
        from alpaca.trading.requests import ClosePositionRequest

        # Clamp a trim to the actual holding. If the clamp consumes the whole position,
        # do a full liquidation instead of a qty-specific close.
        available = get_position_qty(symbol)
        qty = reconcile_sell_qty(qty, available)
        logger.info("CLOSE %s: requested partial, broker_qty=%s, closing qty=%s", symbol, available, qty)
        if qty <= 0:
            return {"symbol": symbol, "qty": 0, "broker_order_id": "", "status": "no_position"}
        if qty >= available:
            order = trading_client().close_position(symbol)
        else:
            order = trading_client().close_position(symbol, ClosePositionRequest(qty=str(qty)))
    else:
        order = trading_client().close_position(symbol)
    return {"symbol": symbol, "qty": qty, "broker_order_id": str(getattr(order, "id", "")),
            "status": str(getattr(getattr(order, "status", None), "value", "submitted"))}


def close_all_positions() -> None:
    trading_client().close_all_positions(cancel_orders=True)


def get_open_order_symbols() -> list[str]:
    """Symbols with currently-open (pending) broker orders — monitored by the AI monitor."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    orders = trading_client().get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    return list({o.symbol for o in orders})


# Order statuses that represent a still-working (pending) order.
_OPEN_ORDER_STATUSES = {
    "new", "accepted", "held", "partially_filled", "pending_new", "accepted_for_bidding",
    "pending_replace", "replaced", "calculated",
}


def get_order_status_counts(limit: int = 500) -> dict:
    """Counts of recent orders grouped by status (filled / canceled / rejected / expired /
    pending...). Pending orders do NOT become positions until they fill."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    orders = trading_client().get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit))
    by_status: dict[str, int] = {}
    for o in orders:
        s = str(o.status.value)
        by_status[s] = by_status.get(s, 0) + 1
    pending = sum(n for s, n in by_status.items() if s in _OPEN_ORDER_STATUSES)
    return {
        "by_status": by_status,
        "pending": pending,
        "filled": by_status.get("filled", 0),
        "canceled": by_status.get("canceled", 0) + by_status.get("cancelled", 0),
        "rejected": by_status.get("rejected", 0),
        "expired": by_status.get("expired", 0),
    }
