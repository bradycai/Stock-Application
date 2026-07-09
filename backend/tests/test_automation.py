"""Autonomous-trading gate + eligibility tests. Pure logic, no DB/broker/network."""
from types import SimpleNamespace

from app.services import automation


def fake_settings(*, enabled, is_live=False, confirmation=""):
    return SimpleNamespace(
        auto_trading_enabled=enabled,
        is_live=is_live,
        live_auto_confirmed=(confirmation == "I_UNDERSTAND_THE_RISK"),
        auto_live_trading_confirmation=confirmation,
    )


# --- Requirement 1: disabled by default --------------------------------------
def test_auto_trading_disabled_by_default():
    # Settings() defaults auto_trading_enabled to False.
    from app.config import Settings

    s = Settings(_env_file=None)
    assert s.auto_trading_enabled is False
    allowed, reason = automation.auto_capability(s)
    assert allowed is False
    assert "AUTO_TRADING_ENABLED" in reason


def test_capability_allows_when_paper_and_enabled():
    allowed, _ = automation.auto_capability(fake_settings(enabled=True, is_live=False))
    assert allowed is True


# --- Requirement 2: no autonomous live trading without all three keys --------
def test_cannot_auto_trade_live_without_confirmation():
    allowed, reason = automation.auto_capability(
        fake_settings(enabled=True, is_live=True, confirmation="")
    )
    assert allowed is False
    assert "LIVE" in reason


def test_cannot_auto_trade_live_with_wrong_confirmation():
    allowed, _ = automation.auto_capability(
        fake_settings(enabled=True, is_live=True, confirmation="yes")
    )
    assert allowed is False


def test_live_allowed_only_with_exact_confirmation():
    allowed, _ = automation.auto_capability(
        fake_settings(enabled=True, is_live=True, confirmation="I_UNDERSTAND_THE_RISK")
    )
    assert allowed is True


# --- Requirement 3: eligibility rules ----------------------------------------
def _eligible(**over):
    kwargs = dict(
        action="buy", confidence_pct=80, stop_loss_pct=5,
        take_profit_pct=10, risk_passed=True, min_confidence=75,
    )
    kwargs.update(over)
    return automation.should_auto_execute(**kwargs)  # -> (ok, reason, category)


def test_eligible_buy_passes():
    ok, _reason, category = _eligible()
    assert ok is True
    assert category == "eligible"


def test_confidence_below_threshold_does_not_auto_buy():
    ok, reason, category = _eligible(confidence_pct=60)
    assert ok is False
    assert "confidence" in reason
    assert category == "confidence"


def test_risk_rejection_prevents_auto_buy():
    ok, reason, category = _eligible(risk_passed=False)
    assert ok is False
    assert "risk" in reason
    assert category == "risk_engine"


def test_missing_stop_blocks_auto():
    ok, _reason, category = _eligible(stop_loss_pct=0)
    assert ok is False
    assert category == "missing_protection"


def test_missing_take_profit_blocks_auto():
    ok, _reason, category = _eligible(take_profit_pct=0)
    assert ok is False
    assert category == "missing_protection"


def test_watch_action_not_auto_executable():
    ok, _reason, category = _eligible(action="watch")
    assert ok is False
    assert category == "not_actionable"


def test_avoid_action_not_auto_executable():
    ok, _reason, category = _eligible(action="avoid")
    assert ok is False
    assert category == "not_actionable"


def test_sell_action_is_eligible():
    ok, _reason, _category = _eligible(action="sell")
    assert ok is True


# --- Requirement: paper auto-buy at the 50% threshold ------------------------
def test_paper_auto_buy_at_50_threshold():
    # 55% confidence clears a 50% paper threshold...
    ok, _reason, _cat = _eligible(confidence_pct=55, min_confidence=50)
    assert ok is True
    # ...but the same setup would NOT clear the strict 75% live default.
    ok_strict, _r, cat = _eligible(confidence_pct=55, min_confidence=75)
    assert ok_strict is False
    assert cat == "confidence"


def test_below_50_threshold_blocked():
    ok, _reason, cat = _eligible(confidence_pct=45, min_confidence=50)
    assert ok is False
    assert cat == "confidence"
