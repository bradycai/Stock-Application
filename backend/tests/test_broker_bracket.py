"""Bracket-order generation + validation tests.

`validate_buy_bracket` is pure. `submit_bracket_order` is exercised against a fake Alpaca
client (no network) to assert: BUY -> bracket with stop<entry<take; SELL -> simple order
(no inverted bracket); invalid BUY -> rejected BEFORE anything is sent.
"""
import pytest
from alpaca.trading.enums import OrderClass, OrderSide

from app.services import broker


# --------------------------------------------------------------------------- #
# Pure validation                                                             #
# --------------------------------------------------------------------------- #
def test_valid_buy_bracket():
    ok, _ = broker.validate_buy_bracket(entry=100, stop=95, take=110)
    assert ok is True


def test_stop_above_entry_rejected():
    ok, reason = broker.validate_buy_bracket(100, 105, 110)
    assert ok is False and "BELOW entry" in reason


def test_take_below_entry_rejected_when_provided():
    ok, reason = broker.validate_buy_bracket(100, 95, 99)
    assert ok is False and "ABOVE entry" in reason


def test_no_take_is_allowed_now():
    # Fixed take-profit removed — an entry with only a protective stop is valid.
    ok, _ = broker.validate_buy_bracket(100, 95, None)
    assert ok is True


def test_stop_not_below_entry_rejected():
    ok, reason = broker.validate_buy_bracket(100, 100, None)
    assert ok is False and "BELOW entry" in reason


def test_nonpositive_entry_rejected():
    ok, _ = broker.validate_buy_bracket(0, 95, None)
    assert ok is False


# --------------------------------------------------------------------------- #
# submit_bracket_order against a fake client                                  #
# --------------------------------------------------------------------------- #
class _Enum:
    def __init__(self, v):
        self.value = v


class _FakeOrder:
    def __init__(self, req):
        self.id = "ord_1"
        self.client_order_id = req.client_order_id
        self.symbol = req.symbol
        self.side = _Enum(req.side.value if hasattr(req.side, "value") else str(req.side))
        self.qty = req.qty
        self.order_type = _Enum("limit")
        self.limit_price = req.limit_price
        self.status = _Enum("accepted")
        self.submitted_at = None


class _FakeClient:
    def __init__(self):
        self.last_req = None

    def submit_order(self, req):
        self.last_req = req
        return _FakeOrder(req)


@pytest.fixture
def fake_client(monkeypatch):
    fc = _FakeClient()
    monkeypatch.setattr(broker, "trading_client", lambda: fc)
    return fc


def test_buy_builds_oto_with_initial_stop_no_take_profit(fake_client):
    out = broker.submit_bracket_order(
        {"ticker": "AAPL", "action": "buy", "limit_price": 100,
         "stop_loss_percent": 5, "take_profit_percent": 10},
        10, "cid-1",
    )
    req = fake_client.last_req
    assert req.order_class == OrderClass.OTO          # OTO, not bracket
    assert req.side == OrderSide.BUY
    assert req.stop_loss.stop_price == 95.0            # initial protective stop below entry
    assert getattr(req, "take_profit", None) is None   # NO fixed take-profit
    assert req.stop_loss.stop_price < req.limit_price
    assert out["stop_loss_price"] == 95.0 and out["take_profit_price"] is None


def test_sell_is_simple_order_not_bracket(fake_client, monkeypatch):
    monkeypatch.setattr(broker, "get_position_qty", lambda s: 50)   # held
    monkeypatch.setattr(broker, "get_available_qty", lambda s: 50)  # all sellable
    out = broker.submit_bracket_order(
        {"ticker": "AAPL", "action": "sell", "limit_price": 100,
         "stop_loss_percent": 5, "take_profit_percent": 10},
        5, "cid-2",
    )
    req = fake_client.last_req
    assert req.side == OrderSide.SELL
    assert req.order_class not in (OrderClass.BRACKET, OrderClass.OTO)  # plain order
    assert getattr(req, "stop_loss", None) is None
    assert getattr(req, "take_profit", None) is None
    assert out["stop_loss_price"] is None and out["take_profit_price"] is None


def test_invalid_buy_is_not_sent_to_broker(fake_client):
    # stop_loss_percent = 0 -> stop == entry (not below) -> invalid -> reject before sending
    with pytest.raises(broker.BracketValidationError):
        broker.submit_bracket_order(
            {"ticker": "BAD", "action": "buy", "limit_price": 100,
             "stop_loss_percent": 0, "take_profit_percent": 0},
            10, "cid-3",
        )
    assert fake_client.last_req is None  # nothing was submitted to Alpaca


# --------------------------------------------------------------------------- #
# Sell quantity reconciliation (never sell more than is held)                 #
# --------------------------------------------------------------------------- #
def test_reconcile_sell_qty_clamps_to_available():
    # the reported bug: requested 13, only 11 available -> sell 11
    assert broker.reconcile_sell_qty(13, 11) == 11


def test_reconcile_sell_qty_passes_through_when_enough():
    assert broker.reconcile_sell_qty(5, 11) == 5


def test_reconcile_sell_qty_zero_when_none_held():
    assert broker.reconcile_sell_qty(13, 0) == 0


def test_buy_13_partial_fill_11_then_sell_uses_11(fake_client, monkeypatch):
    """Buy 13 requested, only 11 fill -> a later sell must request 11, not 13."""
    monkeypatch.setattr(broker, "get_position_qty", lambda s: 11)
    monkeypatch.setattr(broker, "get_available_qty", lambda s: 11)  # all 11 sellable
    broker.submit_bracket_order(
        {"ticker": "TSM", "action": "sell", "limit_price": 100,
         "stop_loss_percent": 5, "take_profit_percent": 10},
        13, "cid-sell",  # requested 13 (the stale qty)
    )
    req = fake_client.last_req
    assert req.qty == 11           # clamped to broker-reported available holding
    assert req.side == OrderSide.SELL


def test_sell_with_no_position_is_not_sent(fake_client, monkeypatch):
    monkeypatch.setattr(broker, "get_position_qty", lambda s: 0)
    monkeypatch.setattr(broker, "get_available_qty", lambda s: 0)
    with pytest.raises(broker.BracketValidationError):
        broker.submit_bracket_order(
            {"ticker": "TSM", "action": "sell", "limit_price": 100,
             "stop_loss_percent": 5, "take_profit_percent": 10},
            13, "cid-sell-0",
        )
    assert fake_client.last_req is None


def test_position_already_protected_skips_sell(fake_client, monkeypatch):
    """Holds 11 but ALL reserved by open bracket legs -> available 0 -> skip, don't send."""
    monkeypatch.setattr(broker, "get_position_qty", lambda s: 11)
    monkeypatch.setattr(broker, "get_available_qty", lambda s: 0)
    with pytest.raises(broker.PositionAlreadyProtected):
        broker.submit_bracket_order(
            {"ticker": "TSM", "action": "sell", "limit_price": 100,
             "stop_loss_percent": 5, "take_profit_percent": 10},
            11, "cid-prot",
        )
    assert fake_client.last_req is None  # no duplicate sell submitted


def test_partial_availability_clamps_to_available(fake_client, monkeypatch):
    """Holds 20, 8 reserved by open orders -> available 12 -> a sell of 15 submits 12."""
    monkeypatch.setattr(broker, "get_position_qty", lambda s: 20)
    monkeypatch.setattr(broker, "get_available_qty", lambda s: 12)
    broker.submit_bracket_order(
        {"ticker": "TSM", "action": "sell", "limit_price": 100,
         "stop_loss_percent": 5, "take_profit_percent": 10},
        15, "cid-part",
    )
    assert fake_client.last_req.qty == 12
