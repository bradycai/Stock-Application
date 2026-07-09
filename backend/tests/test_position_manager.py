"""AI-managed exit tests: trailing stop, partial ladder, exit decisions, review triggers.
Pure logic — no DB/broker/LLM/network."""
from app.services import monitor, position_manager as pm


# --------------------------------------------------------------------------- #
# Trailing stop                                                               #
# --------------------------------------------------------------------------- #
def test_trailing_stop_only_moves_up():
    # seed
    stop = pm.update_trailing_stop(None, highest_price=100, distance_pct=6)
    assert stop == 94.0
    # new high -> stop rises
    stop = pm.update_trailing_stop(stop, highest_price=110, distance_pct=6)
    assert stop == round(110 * 0.94, 2)  # 103.4
    # price pulls back (lower 'highest' passed in) -> stop must NOT drop
    stop_after = pm.update_trailing_stop(stop, highest_price=105, distance_pct=6)
    assert stop_after == stop  # held, never lowered


def test_trailing_distance_uses_atr_when_available():
    # ATR 3 on a $100 stock -> 2.5*3 = 7.5%
    assert pm.trailing_stop_distance_pct(atr=3.0, price=100, fallback_pct=6) == 7.5


def test_trailing_distance_clamps_band():
    assert pm.trailing_stop_distance_pct(atr=0.2, price=100, fallback_pct=6) == pm.MIN_TRAIL_PCT
    assert pm.trailing_stop_distance_pct(atr=20, price=100, fallback_pct=6) == pm.MAX_TRAIL_PCT


def test_trailing_distance_fallback_without_atr():
    assert pm.trailing_stop_distance_pct(atr=None, price=100, fallback_pct=6) == 6.0


def test_trailing_stop_hit():
    assert pm.is_trailing_stop_hit(94.0, 94.0) is True
    assert pm.is_trailing_stop_hit(95.0, 94.0) is False


# --------------------------------------------------------------------------- #
# Partial profit ladder (+5 / +10 / +15)                                      #
# --------------------------------------------------------------------------- #
LEVELS = [5, 10, 15]


def test_partial_at_plus_5():
    assert pm.next_partial_level(5.0, LEVELS, taken=[]) == 5
    assert pm.next_partial_level(4.9, LEVELS, taken=[]) is None


def test_partial_at_plus_10_after_5_taken():
    assert pm.next_partial_level(11.0, LEVELS, taken=[5]) == 10


def test_partial_at_plus_15_after_5_10_taken():
    assert pm.next_partial_level(16.0, LEVELS, taken=[5, 10]) == 15


def test_partial_not_retaken():
    assert pm.next_partial_level(7.0, LEVELS, taken=[5]) is None  # only +5 reached, already taken


def test_gap_up_takes_highest_eligible_level():
    # jumps straight past +5 and +10 -> take +10 first (highest reached, not yet taken)
    assert pm.next_partial_level(12.0, LEVELS, taken=[]) == 10


def test_partial_qty_is_pct_of_original():
    assert pm.partial_qty(100, 20) == 20
    assert pm.partial_qty(7, 20) == 1  # rounds, min 1


# --------------------------------------------------------------------------- #
# Claude exit decision (sell triggers exit only at >= AI_EXIT_CONFIDENCE)     #
# --------------------------------------------------------------------------- #
def test_claude_sell_triggers_exit():
    assert pm.decide_exit("sell", confidence_pct=70, ai_exit_confidence=65) == "sell"


def test_claude_sell_below_confidence_holds():
    assert pm.decide_exit("sell", confidence_pct=60, ai_exit_confidence=65) == "hold"


def test_claude_hold_does_nothing():
    assert pm.decide_exit("hold", confidence_pct=99, ai_exit_confidence=65) == "hold"


def test_claude_trim_trims():
    assert pm.decide_exit("trim", confidence_pct=10, ai_exit_confidence=65) == "trim"


def test_claude_add_holds():
    assert pm.decide_exit("add", confidence_pct=90, ai_exit_confidence=65) == "hold"


def test_claude_tighten_stop():
    assert pm.decide_exit("tighten_stop", confidence_pct=50, ai_exit_confidence=65) == "tighten_stop"


# --------------------------------------------------------------------------- #
# Review triggers                                                             #
# --------------------------------------------------------------------------- #
def _trigs(**over):
    base = dict(
        price=100, prev_price=100, pnl_pct=0, prev_pnl_pct=0, highest_price=100,
        new_high=False, trailing_stop=90, seconds_since_review=10, recheck_interval=300,
        hold_days=1, max_hold_days=20,
    )
    base.update(over)
    return pm.position_review_triggers(**base)


def test_near_trailing_stop_triggers_review():
    # price just above the trailing stop -> approaching
    assert "NEAR_TRAILING_STOP" in _trigs(price=90.5, trailing_stop=90)


def test_new_high_triggers_review():
    assert "NEW_HIGH" in _trigs(new_high=True)


def test_confidence_decay_triggers_review():
    assert "CONFIDENCE_DECAY" in _trigs(seconds_since_review=400, recheck_interval=300)
    assert "CONFIDENCE_DECAY" in _trigs(seconds_since_review=None)  # never reviewed


def test_max_hold_triggers_review():
    assert "MAX_HOLD" in _trigs(hold_days=25, max_hold_days=20)


def test_price_and_pnl_moves_trigger_review():
    assert "PRICE_MOVE" in _trigs(prev_price=100, price=103)
    assert "PNL_MOVE" in _trigs(prev_pnl_pct=2, pnl_pct=6)


def test_quiet_position_no_review():
    # nothing changed, recently reviewed, not near stop -> no triggers
    assert _trigs(seconds_since_review=10, recheck_interval=300) == []


# --------------------------------------------------------------------------- #
# Daily-loss halt still works (backstop unchanged)                            #
# --------------------------------------------------------------------------- #
def test_daily_loss_halt_still_works():
    assert monitor.daily_loss_breached(-10.0, 10.0) is True
    assert monitor.daily_loss_breached(-4.0, 10.0) is False
