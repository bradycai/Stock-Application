"""Dedupe + position-size clamp tests. Pure logic, no DB/broker/network."""
from decimal import Decimal
from types import SimpleNamespace

from app.config import settings
from app.services import risk
from app.services.risk import RiskInput
from app.services.recommendations import clamp_position_size, dedupe_by_ticker


def intent(ticker, confidence):
    return SimpleNamespace(ticker=ticker, confidence=confidence)


def test_no_duplicate_tickers_in_one_run():
    items = [intent("AAPL", 0.7), intent("NVDA", 0.8), intent("AAPL", 0.6), intent("NVDA", 0.9)]
    out = dedupe_by_ticker(items)
    tickers = [i.ticker for i in out]
    assert tickers == sorted(set(tickers), key=tickers.index)  # unique
    assert len(tickers) == len(set(tickers))
    assert set(tickers) == {"AAPL", "NVDA"}


def test_keeps_highest_confidence_per_ticker():
    items = [intent("OKLO", 0.55), intent("OKLO", 0.82), intent("OKLO", 0.40)]
    out = dedupe_by_ticker(items)
    assert len(out) == 1
    assert out[0].confidence == 0.82


def test_preserves_first_appearance_order():
    items = [intent("MSFT", 0.6), intent("AAPL", 0.6), intent("NVDA", 0.6), intent("AAPL", 0.9)]
    out = dedupe_by_ticker(items)
    assert [i.ticker for i in out] == ["MSFT", "AAPL", "NVDA"]


def test_case_insensitive_dedupe():
    items = [intent("aapl", 0.6), intent("AAPL", 0.9)]
    out = dedupe_by_ticker(items)
    assert len(out) == 1
    assert out[0].confidence == 0.9


def test_empty_passthrough():
    assert dedupe_by_ticker([]) == []


# --- position-size clamp -----------------------------------------------------
def test_clamp_reduces_oversize():
    assert clamp_position_size(8, 5) == 5      # DELL 8% -> 5%
    assert clamp_position_size(7, 5) == 5      # VST 7%  -> 5%


def test_clamp_leaves_within_limit_unchanged():
    assert clamp_position_size(3, 5) == 3
    assert clamp_position_size(5, 5) == 5


def test_clamp_never_increases_or_goes_negative():
    assert clamp_position_size(None, 5) == 0.0
    assert clamp_position_size(-2, 5) == 0.0
    assert clamp_position_size(2, 5) == 2      # never bumped up to the cap


def _risk_for_size(size_pct: float) -> risk.RiskResult:
    intent = {
        "ticker": "DELL", "action": "buy", "order_type": "limit", "limit_price": 100.0,
        "position_size_percent": size_pct, "stop_loss_percent": 5.0, "take_profit_percent": 10.0,
    }
    return risk.evaluate(RiskInput(
        intent=intent, equity=Decimal("100000"), cash=Decimal("100000"),
        open_positions=0, held_symbols=set(), day_pnl_pct=Decimal("0"),
        last_price=Decimal("100"), intraday_change_pct=Decimal("0"),
        is_margin_account=True, is_option=False, trading_halted=False,
    ))


def test_oversize_buy_is_clamped_not_rejected_before_display():
    """An over-max BUY (like DELL 8% / VST 7%) is clamped to the cap so it passes risk
    and shows as awaiting-approval, instead of producing a POSITION_TOO_LARGE card."""
    max_pct = float(settings.max_position_pct)
    over = max_pct + 3

    raw = _risk_for_size(over)
    assert not raw.passed
    assert any(v.code == "POSITION_TOO_LARGE" for v in raw.violations)

    clamped = clamp_position_size(over, max_pct)
    assert clamped <= max_pct
    ok = _risk_for_size(clamped)
    assert ok.passed
    assert not any(v.code == "POSITION_TOO_LARGE" for v in ok.violations)
