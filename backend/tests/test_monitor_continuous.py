"""Continuous-monitor tests: rule-based trigger detection + decision->action planning.
Pure logic — no DB/broker/LLM/network."""
from app.services import monitor
from app.services.triggers import detect_triggers


def types(trigs):
    return {t.type for t in trigs}


# --------------------------------------------------------------------------- #
# Trigger detection                                                           #
# --------------------------------------------------------------------------- #
def snap(**over):
    base = dict(price=100.0, pnl_pct=0.0, rel_volume=1.0, rsi=50.0, macd_hist=0.5,
                news_count=0, stop_price=None, take_price=None)
    base.update(over)
    return base


def test_first_observation_seeds_no_change_triggers():
    # No prev -> only stateless triggers (volume/zone) can fire; none here.
    assert detect_triggers(None, snap()) == []


def test_price_move_trigger():
    prev = snap(price=100)
    cur = snap(price=103)  # +3% >= 2%
    assert "PRICE_MOVE" in types(detect_triggers(prev, cur))
    # under threshold -> no PRICE_MOVE
    assert "PRICE_MOVE" not in types(detect_triggers(prev, snap(price=101)))


def test_pnl_move_trigger():
    prev = snap(pnl_pct=2.0)
    assert "PNL_MOVE" in types(detect_triggers(prev, snap(pnl_pct=6.0)))  # +4 pts
    assert "PNL_MOVE" not in types(detect_triggers(prev, snap(pnl_pct=3.5)))  # +1.5 pts


def test_volume_spike_trigger_stateless():
    assert "VOLUME_SPIKE" in types(detect_triggers(None, snap(rel_volume=2.5)))
    assert "VOLUME_SPIKE" not in types(detect_triggers(None, snap(rel_volume=1.5)))


def test_rsi_overbought_cross():
    assert "RSI_CROSS" in types(detect_triggers(snap(rsi=65), snap(rsi=72)))


def test_rsi_oversold_cross():
    assert "RSI_CROSS" in types(detect_triggers(snap(rsi=35), snap(rsi=28)))


def test_macd_reversal():
    assert "MACD_REVERSAL" in types(detect_triggers(snap(macd_hist=-0.4), snap(macd_hist=0.3)))
    assert "MACD_REVERSAL" in types(detect_triggers(snap(macd_hist=0.4), snap(macd_hist=-0.2)))
    assert "MACD_REVERSAL" not in types(detect_triggers(snap(macd_hist=0.4), snap(macd_hist=0.6)))


def test_stop_zone_trigger():
    # price just above the stop -> approaching
    assert "STOP_ZONE" in types(detect_triggers(None, snap(price=100.5, stop_price=100)))
    # comfortably above stop -> no zone
    assert "STOP_ZONE" not in types(detect_triggers(None, snap(price=110, stop_price=100)))


def test_take_zone_trigger():
    assert "TAKE_ZONE" in types(detect_triggers(None, snap(price=109.5, take_price=110)))
    assert "TAKE_ZONE" not in types(detect_triggers(None, snap(price=101, take_price=110)))


def test_news_trigger():
    assert "NEWS" in types(detect_triggers(snap(news_count=1), snap(news_count=3)))
    assert "NEWS" not in types(detect_triggers(snap(news_count=3), snap(news_count=3)))


# --------------------------------------------------------------------------- #
# plan_monitor_action (decision -> execution plan)                            #
# --------------------------------------------------------------------------- #
def plan(action, conf, has_pos=True, auto_buy=True):
    return monitor.plan_monitor_action(action, conf, 65.0, 50.0, has_pos, auto_buy)


def test_sell_above_confidence_auto_sells():
    p, _ = plan("sell", 70)
    assert p == "auto_sell"


def test_sell_below_confidence_ignored():
    p, reason = plan("sell", 60)
    assert p == "ignored_low_confidence"
    assert "65" in reason


def test_sell_without_position_ignored():
    p, _ = plan("sell", 90, has_pos=False)
    assert p == "ignored_no_position"


def test_trim_above_confidence_auto_trims():
    p, _ = plan("trim", 80)
    assert p == "auto_trim"


def test_buy_above_confidence_when_auto_allowed():
    p, _ = plan("buy", 55, auto_buy=True)
    assert p == "auto_buy"


def test_buy_below_confidence_ignored():
    p, _ = plan("add", 40, auto_buy=True)
    assert p == "ignored_low_confidence"


def test_buy_when_auto_gate_off_goes_manual():
    p, _ = plan("buy", 90, auto_buy=False)
    assert p == "manual_review"


def test_hold_is_hold():
    assert plan("hold", 99)[0] == "hold"


def test_tighten_stop_and_raise_take_pass_through():
    assert plan("tighten_stop", 10)[0] == "tighten_stop"
    assert plan("raise_take_profit", 10)[0] == "raise_take_profit"


# --------------------------------------------------------------------------- #
# Capability gate (paper-only unless live confirmed)                          #
# --------------------------------------------------------------------------- #
from types import SimpleNamespace  # noqa: E402


def test_continuous_capability_off_by_default():
    s = SimpleNamespace(continuous_monitor_enabled=False, is_live=False, live_auto_confirmed=False)
    ok, _ = monitor.continuous_capability(s)
    assert ok is False


def test_continuous_capability_paper_on():
    s = SimpleNamespace(continuous_monitor_enabled=True, is_live=False, live_auto_confirmed=False)
    assert monitor.continuous_capability(s)[0] is True


def test_continuous_capability_live_requires_confirmation():
    s = SimpleNamespace(continuous_monitor_enabled=True, is_live=True, live_auto_confirmed=False)
    assert monitor.continuous_capability(s)[0] is False
    s2 = SimpleNamespace(continuous_monitor_enabled=True, is_live=True, live_auto_confirmed=True)
    assert monitor.continuous_capability(s2)[0] is True
