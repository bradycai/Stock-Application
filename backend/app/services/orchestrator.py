"""Glue between intents, live account/market state, and the risk engine.

Used both when Claude first recommends (to filter intents) and again at approval time
(to re-validate against fresh state).
"""
from decimal import Decimal

from app.services import broker, risk


def build_risk_input(
    intent: dict,
    account: dict,
    held_symbols: set[str],
    trading_halted: bool,
    market: dict | None = None,
) -> risk.RiskInput:
    """Construct a RiskInput. `market` is the per-ticker data dict if already gathered;
    otherwise we fetch a fresh quote (used at approval time)."""
    if market is None:
        market = broker.get_quote_and_intraday(intent["ticker"])

    return risk.RiskInput(
        intent=intent,
        equity=Decimal(str(account["equity"])),
        cash=Decimal(str(account["cash"])),
        open_positions=account.get("open_positions", len(held_symbols)),
        held_symbols=held_symbols,
        day_pnl_pct=Decimal(str(account["day_pnl_pct"])),
        last_price=Decimal(str(market.get("last_price") or intent.get("limit_price") or 0)),
        intraday_change_pct=Decimal(str(market.get("intraday_change_pct") or 0)),
        is_margin_account=account.get("is_margin", False),
        is_option=False,  # we never construct option intents; equities only
        trading_halted=trading_halted,
    )
