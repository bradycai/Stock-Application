"""Fail-closed semantic validation of Claude's trade intents."""
import pytest

from app.services.intent_validation import validate_intent


def _intent(**over):
    base = dict(ticker="AAPL", action="buy", order_type="limit", limit_price=100.0,
                position_size_percent=5, stop_loss_percent=5, take_profit_percent=10,
                confidence=0.7, reason="trend + catalyst", risks="loses 50-day SMA",
                evidence=["rsi_14=55.2"])
    base.update(over)
    return base


def _codes(intent, last_price=100.0, age=10.0):
    return [v["code"] for v in validate_intent(intent, last_price=last_price, data_age_sec=age,
                                               max_data_age_sec=600, max_limit_deviation_pct=10)]


def test_well_formed_buy_passes():
    assert _codes(_intent()) == []


@pytest.mark.parametrize("action", ["watch", "avoid"])
def test_non_actionable_intents_are_not_policy_checked(action):
    assert _codes(_intent(action=action, limit_price=0, stop_loss_percent=0, evidence=[]), age=99999) == []


def test_stale_market_data_fails_closed():
    assert "STALE_DATA" in _codes(_intent(), age=601)


@pytest.mark.parametrize("limit,last,code", [
    (0, 100.0, "INVALID_LIMIT_PRICE"),
    (100.0, None, "NO_MARKET_PRICE"),
    (100.0, 0, "NO_MARKET_PRICE"),
    (115.0, 100.0, "LIMIT_PRICE_OFF_MARKET"),   # fabricated / mistyped price
    (85.0, 100.0, "LIMIT_PRICE_OFF_MARKET"),
])
def test_limit_price_must_be_near_the_market(limit, last, code):
    assert code in _codes(_intent(limit_price=limit), last_price=last)


def test_limit_price_within_band_passes():
    assert _codes(_intent(limit_price=109.9)) == []


@pytest.mark.parametrize("field,value,code", [
    ("stop_loss_percent", 0, "INVALID_STOP_LOSS"),
    ("stop_loss_percent", 75, "INVALID_STOP_LOSS"),
    ("take_profit_percent", 0, "INVALID_TAKE_PROFIT"),
    ("take_profit_percent", 900, "INVALID_TAKE_PROFIT"),
    ("reason", "  ", "MISSING_REASON"),
    ("risks", "", "MISSING_RISKS"),
    ("evidence", [], "MISSING_EVIDENCE"),
    ("evidence", ["  "], "MISSING_EVIDENCE"),
    ("evidence", [f"x={i}" for i in range(20)], "TOO_MUCH_EVIDENCE"),
])
def test_required_fields_and_bounds(field, value, code):
    assert code in _codes(_intent(**{field: value}))


def test_sell_is_held_to_the_same_rules():
    assert "MISSING_EVIDENCE" in _codes(_intent(action="sell", evidence=[]))
