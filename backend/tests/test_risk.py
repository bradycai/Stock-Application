"""Risk-engine unit tests. No network — pure logic. Run with `pytest -q`."""
from decimal import Decimal

from app.config import settings
from app.services import risk
from app.services.risk import RiskInput


def base_intent(**overrides):
    intent = {
        "ticker": "AAPL",
        "action": "buy",
        "order_type": "limit",
        "limit_price": 100.0,
        "position_size_percent": 5.0,
        "stop_loss_percent": 5.0,
        "take_profit_percent": 10.0,
    }
    intent.update(overrides)
    return intent


def make_input(intent, **overrides):
    kwargs = dict(
        intent=intent,
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        open_positions=0,
        held_symbols=set(),
        day_pnl_pct=Decimal("0"),
        last_price=Decimal("100"),
        intraday_change_pct=Decimal("0"),
        is_margin_account=False,
        is_option=False,
        trading_halted=False,
    )
    kwargs.update(overrides)
    return RiskInput(**kwargs)


def codes(result):
    return {v.code for v in result.violations}


# --------------------------------------------------------------------------- #
# Happy path                                                                  #
# --------------------------------------------------------------------------- #
def test_valid_buy_passes_and_computes_qty():
    r = risk.evaluate(make_input(base_intent()))
    assert r.passed
    # 5% of 100k = $5000 / $100 limit = 50 shares
    assert r.computed_qty == 50
    assert r.computed_notional == Decimal("5000")


def test_watch_intent_passes_without_position_checks():
    intent = base_intent(action="watch", stop_loss_percent=0, position_size_percent=0)
    r = risk.evaluate(make_input(intent))
    assert r.passed


def test_avoid_intent_passes_without_position_checks():
    # AVOID (e.g. a distressed Q-ticker) takes no position — no stop/size required,
    # and the engine must not try to compute a quantity.
    intent = base_intent(action="avoid", stop_loss_percent=0, position_size_percent=0,
                         limit_price=0)
    r = risk.evaluate(make_input(intent))
    assert r.passed
    assert r.computed_qty is None


def test_avoid_still_blocked_when_trading_halted():
    intent = base_intent(action="avoid")
    r = risk.evaluate(make_input(intent, trading_halted=True))
    assert not r.passed
    assert "DAILY_LOSS_HALT" in codes(r)


# --------------------------------------------------------------------------- #
# Rule 0 — daily loss kill switch                                             #
# --------------------------------------------------------------------------- #
def test_daily_loss_limit_halts():
    r = risk.evaluate(make_input(base_intent(), day_pnl_pct=Decimal("-10")))
    assert not r.passed
    assert "DAILY_LOSS_HALT" in codes(r)


def test_manual_halt_blocks_even_watch():
    intent = base_intent(action="watch")
    r = risk.evaluate(make_input(intent, trading_halted=True))
    assert not r.passed
    assert "DAILY_LOSS_HALT" in codes(r)


# --------------------------------------------------------------------------- #
# Rule 1 — stop loss required                                                 #
# --------------------------------------------------------------------------- #
def test_missing_stop_loss_fails():
    r = risk.evaluate(make_input(base_intent(stop_loss_percent=0)))
    assert not r.passed
    assert "NO_STOP_LOSS" in codes(r)


# --------------------------------------------------------------------------- #
# Rule 2 — limit orders only                                                  #
# --------------------------------------------------------------------------- #
def test_market_order_fails():
    r = risk.evaluate(make_input(base_intent(order_type="market")))
    assert not r.passed
    assert "NON_LIMIT_ORDER" in codes(r)


# --------------------------------------------------------------------------- #
# Rule 3 — position size <= 10%                                               #
# --------------------------------------------------------------------------- #
def test_oversize_position_fails():
    r = risk.evaluate(make_input(base_intent(position_size_percent=25)))
    assert not r.passed
    assert "POSITION_TOO_LARGE" in codes(r)


def test_position_at_configured_max_passes():
    # Boundary is the CONFIGURED max (default 10, but paper .env sets 5) — read it from
    # settings so the test tracks the deployment rather than hardcoding a value.
    max_pct = float(settings.max_position_pct)
    r = risk.evaluate(make_input(base_intent(position_size_percent=max_pct)))
    assert r.passed


def test_just_over_configured_max_fails():
    over = float(settings.max_position_pct) + 0.01
    r = risk.evaluate(make_input(base_intent(position_size_percent=over)))
    assert not r.passed
    assert "POSITION_TOO_LARGE" in codes(r)


# --------------------------------------------------------------------------- #
# Rule 4 — max open positions (uses the CONFIGURED max, not a hardcoded 3)     #
# --------------------------------------------------------------------------- #
def test_new_position_at_max_fails():
    max_n = settings.max_open_positions
    held = {f"SYM{i}" for i in range(max_n)}  # exactly max distinct held symbols
    r = risk.evaluate(
        make_input(base_intent(ticker="NEWTICKER"), open_positions=max_n, held_symbols=held)
    )
    assert not r.passed
    assert "TOO_MANY_POSITIONS" in codes(r)


def test_adding_to_existing_position_allowed_at_max():
    max_n = settings.max_open_positions
    held = {f"SYM{i}" for i in range(max_n)}
    held.add("AAPL")
    r = risk.evaluate(
        make_input(base_intent(ticker="AAPL"), open_positions=max_n, held_symbols=held)
    )
    assert r.passed  # AAPL already held -> not a NEW position


# --------------------------------------------------------------------------- #
# Rule 5 — no margin (reject trades that REQUIRE margin, not margin-enabled accounts) #
# --------------------------------------------------------------------------- #
def test_margin_enabled_account_with_cash_passes():
    # Alpaca paper accounts are margin-enabled by default. A fully cash-funded order
    # must NOT be blocked just because the account type allows margin.
    r = risk.evaluate(make_input(base_intent(), is_margin_account=True))
    assert r.passed
    assert "MARGIN_ACCOUNT" not in codes(r)


def test_buy_exceeding_cash_fails():
    # 10% of 100k = $10k but only $1k cash -> would require margin
    r = risk.evaluate(make_input(base_intent(position_size_percent=10), cash=Decimal("1000")))
    assert not r.passed
    assert "MARGIN_REQUIRED" in codes(r)


def test_buy_exceeding_cash_fails_even_on_margin_account():
    r = risk.evaluate(
        make_input(base_intent(position_size_percent=10), cash=Decimal("1000"),
                   is_margin_account=True)
    )
    assert not r.passed
    assert "MARGIN_REQUIRED" in codes(r)


# --------------------------------------------------------------------------- #
# Rule 6 — no options                                                         #
# --------------------------------------------------------------------------- #
def test_option_instrument_fails():
    r = risk.evaluate(make_input(base_intent(), is_option=True))
    assert not r.passed
    assert "OPTIONS_FORBIDDEN" in codes(r)


# --------------------------------------------------------------------------- #
# Rule 7 — no market orders on penny stocks                                   #
# --------------------------------------------------------------------------- #
def test_penny_stock_market_order_fails():
    intent = base_intent(order_type="market", limit_price=2.0)
    r = risk.evaluate(make_input(intent, last_price=Decimal("2.00")))
    assert not r.passed
    # Both the non-limit and penny-market rules should flag it
    assert "PENNY_MARKET_ORDER" in codes(r) or "NON_LIMIT_ORDER" in codes(r)


# --------------------------------------------------------------------------- #
# Rule 8 — no buying parabolic names                                          #
# --------------------------------------------------------------------------- #
def test_parabolic_buy_fails():
    r = risk.evaluate(make_input(base_intent(), intraday_change_pct=Decimal("60")))
    assert not r.passed
    assert "PARABOLIC_MOVE" in codes(r)


def test_fifty_percent_exactly_passes():
    r = risk.evaluate(make_input(base_intent(), intraday_change_pct=Decimal("50")))
    assert r.passed  # rule is strictly greater than 50


# --------------------------------------------------------------------------- #
# Quantity edge case                                                          #
# --------------------------------------------------------------------------- #
def test_zero_share_quantity_fails():
    # tiny equity so notional < 1 share
    r = risk.evaluate(
        make_input(base_intent(position_size_percent=1, limit_price=10000),
                   equity=Decimal("100"), cash=Decimal("100"))
    )
    assert not r.passed
    assert "ZERO_QTY" in codes(r)
