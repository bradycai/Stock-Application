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
    db = SimpleNamespace(get=lambda *_a, **_k: state, flush=lambda: None)
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
def _use_gate(monkeypatch, **gate_kwargs):
    monkeypatch.setattr(automation, "order_gate", lambda db: _gate(**gate_kwargs))


def test_may_order_allows_only_when_gate_allows(monkeypatch):
    db = _db()
    _use_gate(monkeypatch, mode="auto")
    assert monitor._may_order(db, "trailing_stop", "AAPL") is True
    _use_gate(monkeypatch, mode="manual")
    assert monitor._may_order(db, "trailing_stop", "AAPL") is False
    assert db.added == []  # manual mode is logged, not audited


def test_may_order_rereads_the_gate_for_every_order(monkeypatch):
    # A kill switch committed mid-cycle must stop the very next order.
    gates = iter([_gate(mode="auto"), _gate(mode="auto", kill_switch=True)])
    monkeypatch.setattr(automation, "order_gate", lambda db: next(gates))
    assert monitor._may_order(_db(), "trailing_stop", "AAPL") is True
    assert monitor._may_order(_db(), "partial_5pct", "MSFT") is False


def test_order_gate_bypasses_the_session_cache():
    seen = {}
    state = SimpleNamespace(mode="manual", kill_switch=False, kill_switch_reason=None)

    def get(_model, _id, **kwargs):
        seen.update(kwargs)
        return state
    automation.order_gate(SimpleNamespace(get=get))
    assert seen.get("populate_existing") is True


def test_release_always_leaves_manual_even_after_a_racing_mode_change():
    state = SimpleNamespace(mode="auto", kill_switch=True, kill_switch_reason="x", kill_switch_at=None,
                            kill_switch_by=None, updated_at=None, updated_by=None)
    db = SimpleNamespace(get=lambda *_a, **_k: state, flush=lambda: None)
    automation.set_kill_switch(db, False, "user:operator")
    assert state.kill_switch is False and state.mode == "manual"


def test_observe_mode_records_would_execute_once_per_cooldown(monkeypatch):
    monkeypatch.setattr(monitor, "_LAST_OBSERVED", {})
    _use_gate(monkeypatch, mode="observe")
    db = _db()
    assert monitor._may_order(db, "trailing_stop", "AAPL", price=10.0) is False
    assert monitor._may_order(db, "trailing_stop", "AAPL", price=9.9) is False
    assert [a.event_type for a in db.added] == ["observe_would_execute"]
    assert db.added[0].payload["symbol"] == "AAPL"


def test_blocked_managed_sell_sends_nothing_and_keeps_position_open(monkeypatch, no_broker):
    _use_gate(monkeypatch, mode="manual")
    pm = SimpleNamespace(is_open=True, closed_at=None, last_decision=None)
    counts = {"ignored": 0, "errors": 0, "auto_sells": 0}
    sent = monitor._managed_sell_all(_db(), pm, "AAPL", "trailing_stop", counts, 95.0)
    assert sent is False and pm.is_open is True and counts["ignored"] == 1


def test_blocked_partial_does_not_mark_level_taken(monkeypatch, no_broker):
    _use_gate(monkeypatch, mode="observe")
    pm = SimpleNamespace(trailing_stop_price=90.0, broker_stop_price=None)
    counts = {"ignored": 0, "errors": 0, "partials": 0}
    sent = monitor._managed_sell_partial(_db(), pm, "AAPL", {"qty": 10}, 2, "partial_5pct", counts, 105.0)
    assert sent is False and counts["partials"] == 0


# --------------------------------------------------------------------------- #
# Review regressions                                                          #
# --------------------------------------------------------------------------- #
def test_daily_loss_liquidates_before_dropping_to_manual(monkeypatch):
    state = SimpleNamespace(mode="auto")
    monkeypatch.setattr(automation, "order_gate", lambda db: _gate(mode=state.mode))
    monkeypatch.setattr(automation, "get_state", lambda db, **k: state)
    monkeypatch.setattr(automation, "set_mode", lambda db, mode, actor: setattr(state, "mode", mode))
    monkeypatch.setattr(monitor.risk_state, "halt", lambda *a, **k: None)
    monkeypatch.setattr(broker, "get_account", lambda: {"day_pnl_pct": -12.0, "equity": 1.0})
    monkeypatch.setattr(broker, "get_positions", lambda: [{"symbol": "AAPL"}, {"symbol": "MSFT"}])
    closed = []
    monkeypatch.setattr(broker, "close_position", lambda sym, qty=None: closed.append((sym, state.mode)))
    db = SimpleNamespace(add=lambda _: None, commit=lambda: None)
    result = monitor.check_once(db)
    assert result["halted"] is True
    assert closed == [("AAPL", "auto"), ("MSFT", "auto")]  # sold while still permitted
    assert state.mode == "manual"


def test_monitor_buy_with_off_market_price_or_absurd_stop_is_never_approvable():
    from app.schemas import MonitorDecision
    from app.services.triggers import Trigger

    decision = MonitorDecision(ticker="AAPL", action="buy", confidence=0.55, reason="breakout",
                               size_percent=5, stop_loss_percent=95, take_profit_percent=1000,
                               limit_price=130.0)
    db = SimpleNamespace(add=lambda _: None, flush=lambda: None)
    account = {"equity": 100_000, "cash": 100_000, "day_pnl_pct": 0.0, "open_positions": 0}
    row = monitor._persist_buy_intent(
        db, SimpleNamespace(id=1), "AAPL", decision, {"price": 100.0},
        {"last_price": 100.0, "intraday_change_pct": 0.0}, account, set(), False,
        fired=[Trigger("VOLUME_SPIKE", "relative volume 3.0x")], data_age_sec=5,
    )
    codes = {v["code"] for v in row.risk_violations}
    assert row.status == IntentStatus.rejected_by_risk and row.risk_passed is False
    assert {"LIMIT_PRICE_OFF_MARKET", "INVALID_STOP_LOSS", "INVALID_TAKE_PROFIT"} <= codes
    assert row.computed_qty is None


def test_continuous_cycle_isolates_a_failing_symbol(monkeypatch, no_broker):
    """One symbol crashing rolls back only its own work; earlier symbols stay committed,
    and a monitor buy in observe mode stays pending without any broker call."""
    from app.schemas import MonitorDecision

    monkeypatch.setattr(monitor, "_LAST_OBSERVED", {})
    monkeypatch.setattr(monitor, "_LAST_STATE", {})
    _use_gate(monkeypatch, mode="observe")
    monkeypatch.setattr(broker, "get_open_order_symbols", lambda: [])
    monkeypatch.setattr(monitor.risk_state, "is_halted", lambda db, acct: (False, None))
    market = {s: {"last_price": 100.0, "intraday_change_pct": 0.0, "news": [],
                  "quality": {"stale": False, "incomplete": False, "missing": []},
                  "indicators": {"relative_volume": 3.0, "rsi_14": 55, "macd": {"histogram": 0.1}}}
              for s in ("AAPL", "BOOM")}
    monkeypatch.setattr(monitor.data_aggregator, "build_context",
                        lambda syms, **kw: ({"watchlist_data": market}, {}))

    def decide(payload):
        if payload["ticker"] == "BOOM":
            raise RuntimeError("unexpected failure")
        return MonitorDecision(ticker="AAPL", action="buy", confidence=0.9, reason="volume breakout",
                               size_percent=5, stop_loss_percent=5, take_profit_percent=10,
                               limit_price=100.5), {}, True
    monkeypatch.setattr(monitor.analyst, "monitor_decision", decide)

    events, added = [], []
    rows = SimpleNamespace(all=lambda: ["AAPL", "BOOM"], first=lambda: None)
    db = SimpleNamespace(scalars=lambda *_a, **_k: rows, add=added.append, flush=lambda: None,
                         commit=lambda: events.append("commit"), rollback=lambda: events.append("rollback"))
    account = {"equity": 100_000, "cash": 100_000, "day_pnl_pct": 0.0, "open_positions": 0}

    counts = monitor._run_continuous(db, account, [], _gate(mode="observe"))

    assert counts["errors"] == 1 and counts["ignored"] == 1
    # data writes committed, then AAPL committed, then only BOOM rolled back
    assert events[:3] == ["commit", "commit", "rollback"]
    intents = [a for a in added if type(a).__name__ == "TradeIntent"]
    assert len(intents) == 1 and intents[0].status == IntentStatus.pending_approval
    assert any(getattr(a, "event_type", "") == "observe_would_execute" for a in added)
