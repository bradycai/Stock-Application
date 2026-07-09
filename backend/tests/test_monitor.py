"""Position-monitor decision tests. Pure logic, no DB/broker/network."""
from app.services import monitor


# --- stop-loss triggers a sell ----------------------------------------------
def test_stop_loss_triggers_sell():
    # entry 100, stop 95; price fell to 94 -> stop_loss
    assert monitor.evaluate_exit(94.0, 95.0, 110.0) == "stop_loss"


def test_at_stop_price_triggers():
    assert monitor.evaluate_exit(95.0, 95.0, 110.0) == "stop_loss"


# --- take-profit triggers a sell --------------------------------------------
def test_take_profit_triggers_sell():
    assert monitor.evaluate_exit(111.0, 95.0, 110.0) == "take_profit"


def test_at_take_price_triggers():
    assert monitor.evaluate_exit(110.0, 95.0, 110.0) == "take_profit"


# --- in-range holds ----------------------------------------------------------
def test_in_range_no_exit():
    assert monitor.evaluate_exit(100.0, 95.0, 110.0) is None


def test_missing_levels_no_exit():
    assert monitor.evaluate_exit(100.0, None, None) is None


def test_zero_price_no_exit():
    assert monitor.evaluate_exit(0.0, 95.0, 110.0) is None


# --- daily-loss halt ---------------------------------------------------------
def test_daily_loss_breached_triggers():
    assert monitor.daily_loss_breached(-10.0, 10.0) is True
    assert monitor.daily_loss_breached(-12.5, 10.0) is True


def test_daily_loss_not_breached():
    assert monitor.daily_loss_breached(-5.0, 10.0) is False
    assert monitor.daily_loss_breached(3.0, 10.0) is False
