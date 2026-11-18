"""Automation modes, kill switch, and the single order gate. No DB, broker or network:
the broker is replaced with stubs that fail the test if an order call is attempted."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.models import IntentAction, IntentStatus
from app.services import automation, broker, execution, monitor

OK = dict(capability_ok=True, capability_reason="ok")


# --------------------------------------------------------------------------- #
# Pure decision table                                                         #
# --------------------------------------------------------------------------- #
def test_auto_mode_with_capability_allows_orders():
    gate = automation.evaluate_gate(mode="auto", kill_switch=False, **OK)
    assert gate.allowed is True and gate.observe is False


@pytest.mark.parametrize("mode", ["manual", "observe", "bogus"])
def test_non_auto_modes_never_allow_orders(mode):
    gate = automation.evaluate_gate(mode=mode, kill_switch=False, **OK)
    assert gate.allowed is False
    assert gate.observe is (mode == "observe")


def test_auto_mode_without_env_capability_is_blocked():
    gate = automation.evaluate_gate(mode="auto", kill_switch=False, capability_ok=False,
                                    capability_reason="AUTO_TRADING_ENABLED is false")
    assert gate.allowed is False and "AUTO_TRADING_ENABLED" in gate.reason


@pytest.mark.parametrize("mode", ["auto", "observe", "manual"])
def test_kill_switch_overrides_every_mode(mode):
    gate = automation.evaluate_gate(mode=mode, kill_switch=True, kill_reason="incident", **OK)
    assert gate.allowed is False and gate.observe is False and gate.kill_switch is True
    assert "incident" in gate.reason


def test_engaging_kill_switch_drops_mode_to_manual_and_release_does_not_resume():
    state = SimpleNamespace(mode="auto", kill_switch=False, kill_switch_reason=None, kill_switch_at=None,
                            kill_switch_by=None, updated_at=None, updated_by=None)
    db = SimpleNamespace(get=lambda *_: state, flush=lambda: None)
    automation.set_kill_switch(db, True, "user:operator", "broker outage")
    assert state.kill_switch is True and state.mode == "manual" and state.kill_switch_by == "user:operator"
    automation.set_kill_switch(db, False, "user:operator")
    assert state.kill_switch is False and state.mode == "manual" and state.kill_switch_reason is None


def test_set_mode_rejects_unknown_modes():
    db = SimpleNamespace(get=lambda *_: SimpleNamespace(mode="manual"), flush=lambda: None)
    with pytest.raises(ValueError):
        automation.set_mode(db, "yolo", "user")


# --------------------------------------------------------------------------- #
# execute_intent enforces the gate (defense in depth)                         #
# --------------------------------------------------------------------------- #
@pytest.fixture()
def no_broker(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("broker must not be called")
    for name in ("get_account", "get_positions", "submit_bracket_order", "close_position",
                 "cancel_open_orders", "place_stop_order"):
        monkeypatch.setattr(broker, name, boom)


def _intent(action="buy"):
    return SimpleNamespace(id=1, ticker="AAPL", action=IntentAction(action),
                           status=IntentStatus.pending_approval, created_at=datetime.now(timezone.utc))


def _db():
    added = []
    return SimpleNamespace(add=added.append, added=added)


def _gate(**kw):
    return automation.evaluate_gate(**{"mode": "manual", "kill_switch": False, **OK, **kw})


@pytest.mark.parametrize("gate_kwargs", [dict(mode="manual"), dict(mode="observe"),
                                         dict(mode="auto", kill_switch=True)])
def test_automated_execution_is_blocked_unless_gate_allows(monkeypatch, no_broker, gate_kwargs):
    monkeypatch.setattr(automation, "order_gate", lambda db: _gate(**gate_kwargs))
    intent, db = _intent(), _db()
    with pytest.raises(execution.ExecutionError) as e:
        execution.execute_intent(db, intent, "automation", auto=True)
    assert e.value.status_code == 423
    assert intent.status == IntentStatus.pending_approval  # left for a human
    assert db.added[0].event_type == "auto_order_blocked"


def test_kill_switch_blocks_manual_buys(monkeypatch, no_broker):
    monkeypatch.setattr(automation, "order_gate", lambda db: _gate(kill_switch=True))
    with pytest.raises(execution.ExecutionError) as e:
        execution.execute_intent(_db(), _intent("buy"), "user:operator", auto=False)
    assert e.value.status_code == 423 and "buys are blocked" in str(e.value.detail)


def test_kill_switch_still_lets_manual_sells_through_the_gate(monkeypatch):
    monkeypatch.setattr(automation, "order_gate", lambda db: _gate(kill_switch=True))
    reached = []

    def account():
        reached.append("broker")
        raise RuntimeError("stop here")  # past the gate is all this test needs
    monkeypatch.setattr(broker, "get_account", account)
    with pytest.raises(execution.ExecutionError) as e:
        execution.execute_intent(_db(), _intent("sell"), "user:operator", auto=False)
    assert reached == ["broker"] and e.value.status_code == 502


# --------------------------------------------------------------------------- #
# Monitor: every direct broker call goes through _may_order                   #
# --------------------------------------------------------------------------- #
def test_may_order_allows_only_when_gate_allows():
    db = _db()
    assert monitor._may_order(db, _gate(mode="auto"), "trailing_stop", "AAPL") is True
    assert monitor._may_order(db, _gate(mode="manual"), "trailing_stop", "AAPL") is False
    assert db.added == []  # manual mode is logged, not audited


def test_observe_mode_records_would_execute_once_per_cooldown(monkeypatch):
    monkeypatch.setattr(monitor, "_LAST_OBSERVED", {})
    db = _db()
    observe = _gate(mode="observe")
    assert monitor._may_order(db, observe, "trailing_stop", "AAPL", price=10.0) is False
    assert monitor._may_order(db, observe, "trailing_stop", "AAPL", price=9.9) is False
    assert [a.event_type for a in db.added] == ["observe_would_execute"]
    assert db.added[0].payload["symbol"] == "AAPL"


def test_blocked_managed_sell_sends_nothing_and_keeps_position_open(no_broker):
    pm = SimpleNamespace(is_open=True, closed_at=None, last_decision=None)
    counts = {"ignored": 0, "errors": 0, "auto_sells": 0}
    sent = monitor._managed_sell_all(_db(), pm, "AAPL", "trailing_stop", counts, 95.0, _gate(mode="manual"))
    assert sent is False and pm.is_open is True and counts["ignored"] == 1


def test_blocked_partial_does_not_mark_level_taken(no_broker):
    pm = SimpleNamespace(trailing_stop_price=90.0, broker_stop_price=None)
    counts = {"ignored": 0, "errors": 0, "partials": 0}
    sent = monitor._managed_sell_partial(_db(), pm, "AAPL", {"qty": 10}, 2, "partial_5pct", counts, 105.0,
                                         _gate(mode="observe"))
    assert sent is False and counts["partials"] == 0
