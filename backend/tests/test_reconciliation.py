"""Issue #13: exit recording, broker reconciliation, kill-switch cancels, entry blocking.
Every broker call is a fake: any order-placing call that a test does not expect fails it.
DB tests use the `pg_db` fixture (skipped without TEST_DATABASE_URL)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import (
    AnalysisRun,
    AuditLog,
    IntentAction,
    IntentStatus,
    Order,
    PositionManagement,
    TradeIntent,
    TradeOutcome,
)
from app.routers import automation as automation_router
from app.schemas import KillSwitchRequest
from app.services import (
    automation,
    broker,
    exits,
    execution,
    market_data,
    monitor,
    order_sync,
    reconcile,
)

UTC = timezone.utc
T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)


class NotFound(Exception):
    status_code = 404


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #
def _run(db) -> AnalysisRun:
    run = AnalysisRun(model="m", input_context={}, raw_response={})
    db.add(run)
    db.flush()
    return run


def _intent(db, run, ticker="AAPL", action=IntentAction.buy, status=IntentStatus.pending_approval,
            created_at=None, **kw) -> TradeIntent:
    i = TradeIntent(run_id=run.id, ticker=ticker, action=action, status=status,
                    created_at=created_at or datetime.now(UTC), **kw)
    db.add(i)
    db.flush()
    return i


def _order(db, intent, *, side="buy", status="new", broker_id="b1", qty=10, symbol="AAPL",
           exit_reason=None, stop=None) -> Order:
    o = Order(intent_id=intent.id, broker_order_id=broker_id, client_order_id=f"c-{broker_id}",
              symbol=symbol, side=side, qty=qty, order_type="limit", status=status, is_live=False,
              exit_reason=exit_reason, stop_loss_price=stop)
    db.add(o)
    db.flush()
    return o


def _details(status="filled", filled_qty=10, price=100.0, filled_at=T0, side="buy"):
    return {"status": status, "filled_qty": filled_qty, "filled_avg_price": price,
            "filled_at": filled_at, "side": side, "order_type": "market", "legs": []}


def _audits(db, event):
    return db.scalars(select(AuditLog).where(AuditLog.event_type == event)).all()


@pytest.fixture()
def fake(monkeypatch):
    """Fake broker. Order-placing calls are recorded; anything not faked fails the test."""
    st = SimpleNamespace(details={}, closed=[], positions=[], open_orders=[], account_error=None,
                         close_result=lambda sym, qty: {"symbol": sym, "qty": qty,
                                                        "broker_order_id": f"x-{sym}-{qty}",
                                                        "client_order_id": "", "status": "accepted"})

    def close_position(symbol, qty=None):
        st.closed.append((symbol, qty))
        return st.close_result(symbol, qty)

    def get_order_details(oid):
        d = st.details[oid]
        if isinstance(d, Exception):
            raise d
        return d

    def get_positions():
        if st.account_error:
            raise st.account_error
        return st.positions

    monkeypatch.setattr(broker, "close_position", close_position)
    monkeypatch.setattr(broker, "cancel_open_orders", lambda s: 0)
    monkeypatch.setattr(broker, "place_stop_order", lambda *a, **k: {"placed": True})
    monkeypatch.setattr(broker, "get_order_details", get_order_details)
    monkeypatch.setattr(broker, "get_positions", get_positions)
    monkeypatch.setattr(broker, "list_open_orders", lambda: st.open_orders)
    monkeypatch.setattr(broker, "submit_bracket_order",
                        lambda *a, **k: pytest.fail("unexpected order submission"))
    monkeypatch.setattr(broker, "close_all_positions", lambda: pytest.fail("close_all attempted"))
    return st


@pytest.fixture()
def auto_gate(monkeypatch):
    gate = automation.evaluate_gate(mode="auto", kill_switch=False, capability_ok=True,
                                    capability_reason="ok")
    monkeypatch.setattr(automation, "order_gate", lambda db: gate)


def _open_trade(db, fake, *, qty=10, price=100.0):
    """A filled entry with an open trade outcome and an open managed position."""
    intent = _intent(db, _run(db), status=IntentStatus.submitted)
    entry = _order(db, intent, status="new", broker_id="buy1", qty=qty, stop=95.0)
    fake.details["buy1"] = _details(filled_qty=qty, price=price)
    db.commit()
    order_sync.sync_orders(db)
    pm = PositionManagement(symbol="AAPL", entry_price=price, original_qty=qty, highest_price=price,
                            trailing_stop_price=95.0, intent_id=intent.id, partial_levels_taken=[])
    db.add(pm)
    db.commit()
    return intent, entry, pm


def _counts():
    return {"ignored": 0, "errors": 0, "partials": 0, "auto_sells": 0}


# --------------------------------------------------------------------------- #
# 1. every exit path is journaled                                             #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reason", ["trailing_stop", "claude_sell"])
def test_full_exit_is_recorded_and_fill_closes_the_outcome(pg_db, fake, auto_gate, reason):
    _intent_row, _entry, pm = _open_trade(pg_db, fake)
    sent = monitor._managed_sell_all(pg_db, pm, "AAPL", reason, _counts(), 90.0, qty=10)
    pg_db.commit()
    assert sent and fake.closed == [("AAPL", None)]

    order = pg_db.scalars(select(Order).where(Order.side == "sell")).one()
    assert order.broker_order_id == "x-AAPL-None"
    assert order.exit_reason == reason and order.qty == 10 and order.status == "accepted"
    exit_intent = pg_db.get(TradeIntent, order.intent_id)
    assert exit_intent.strategy_id == "monitor-exit" and exit_intent.action == IntentAction.sell
    assert exit_intent.status == IntentStatus.submitted
    assert any("closes_entry_intent" in e for e in exit_intent.evidence)
    assert _audits(pg_db, "exit_order_recorded")

    fake.details[order.broker_order_id] = _details(price=90.0, side="sell",
                                                   filled_at=T0 + timedelta(days=1))
    order_sync.sync_orders(pg_db)
    out = pg_db.scalars(select(TradeOutcome)).one()
    assert out.status == "closed" and out.exit_reason == reason
    assert out.exit_order_id == order.id and float(out.realized_pnl) == -100.0


def test_partial_exit_is_recorded_but_its_fill_keeps_the_outcome_open(pg_db, fake, auto_gate):
    _i, _e, pm = _open_trade(pg_db, fake)
    counts = _counts()
    assert monitor._managed_sell_partial(pg_db, pm, "AAPL", {"qty": 10}, 2, "partial_5pct", counts, 105.0)
    pg_db.commit()
    order = pg_db.scalars(select(Order).where(Order.side == "sell")).one()
    assert order.exit_reason == "partial_5pct" and order.qty == 2

    fake.details[order.broker_order_id] = _details(filled_qty=2, price=105.0, side="sell")
    order_sync.sync_orders(pg_db)
    assert pg_db.scalars(select(TradeOutcome)).one().status == "open"  # shares still held
    assert pg_db.scalars(select(Order).where(Order.id == order.id)).one().status == "filled"


def test_stop_backstop_exit_is_recorded(pg_db, fake, auto_gate, monkeypatch):
    _i, entry, _pm = _open_trade(pg_db, fake)
    monkeypatch.setattr(broker, "get_account", lambda: {"day_pnl_pct": 0.0, "equity": 1e5})
    fake.positions = [{"symbol": "AAPL", "qty": 10, "current_price": 94.0, "avg_entry_price": 100.0,
                       "unrealized_plpc": -6.0}]
    monitor.check_once(pg_db)
    sells = pg_db.scalars(select(Order).where(Order.side == "sell")).all()
    assert [(o.exit_reason, o.symbol) for o in sells] == [("stop_loss", "AAPL")]
    pg_db.refresh(entry)
    assert entry.monitoring_status == "exited" and entry.exit_reason == "stop_loss"
    # the recorded exit must not shadow the entry's protective levels
    assert monitor._latest_protective_order(pg_db, "AAPL").id == entry.id


def test_daily_loss_liquidation_is_recorded(pg_db, fake, auto_gate, monkeypatch):
    _open_trade(pg_db, fake)
    monkeypatch.setattr(broker, "get_account", lambda: {"day_pnl_pct": -15.0, "equity": 1e5})
    fake.positions = [{"symbol": "AAPL", "qty": 10, "current_price": 85.0, "avg_entry_price": 100.0,
                       "unrealized_plpc": -15.0}]
    result = monitor.check_once(pg_db)
    assert result["halted"] and result["closed"] == ["AAPL"]
    sell = pg_db.scalars(select(Order).where(Order.side == "sell")).one()
    assert sell.exit_reason == "daily_loss"


def test_record_exit_skips_when_no_order_was_placed_and_is_idempotent(pg_db):
    assert exits.record_exit(pg_db, symbol="AAPL", reason="x", qty=1, is_live=False,
                             result={"broker_order_id": "", "status": "no_position"}) is None
    assert exits.record_exit(pg_db, symbol="AAPL", reason="x", qty=1, is_live=False, result=None) is None
    res = {"broker_order_id": "dup1", "status": "accepted", "qty": 3}
    a = exits.record_exit(pg_db, symbol="AAPL", reason="claude_trim", qty=3, is_live=False, result=res)
    b = exits.record_exit(pg_db, symbol="AAPL", reason="claude_trim", qty=3, is_live=False, result=res)
    assert a.id == b.id and len(pg_db.scalars(select(Order)).all()) == 1  # no entry intent: own run


def test_failed_journal_write_never_breaks_the_cycle(pg_db, monkeypatch):
    monkeypatch.setattr(exits, "record_exit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    monitor._record_exit_safe(pg_db, "AAPL", {"broker_order_id": "z"}, "trailing_stop", 1)
    assert _audits(pg_db, "exit_record_failed")


# --------------------------------------------------------------------------- #
# 2. reconciliation                                                           #
# --------------------------------------------------------------------------- #
def test_reconcile_closes_managed_position_no_longer_held(pg_db, fake):
    _i, _e, pm = _open_trade(pg_db, fake)
    fake.positions = []
    counts = reconcile.reconcile(pg_db)
    pg_db.refresh(pm)
    assert counts["positions_closed"] == 1 and pm.is_open is False and pm.closed_at is not None
    assert pm.last_decision == "reconciled_not_held_at_broker"
    assert _audits(pg_db, "reconcile_position_closed")


def test_reconcile_keeps_held_position_and_does_not_infer_a_requested_sell(pg_db, fake, auto_gate):
    _i, _e, pm = _open_trade(pg_db, fake)
    fake.positions = [{"symbol": "AAPL", "qty": 10}]
    # A sell was REQUESTED (order recorded, still working) but the broker still holds shares.
    monitor._record_exit_safe(pg_db, "AAPL", {"broker_order_id": "sellx", "status": "accepted"},
                              "trailing_stop", 10)
    pg_db.commit()
    fake.open_orders = [{"broker_order_id": "sellx", "symbol": "AAPL", "side": "sell"}]
    counts = reconcile.reconcile(pg_db)
    pg_db.refresh(pm)
    assert counts["positions_closed"] == 0 and pm.is_open is True
    assert pg_db.scalars(select(TradeOutcome)).one().status == "open"


def test_reconcile_flags_unknown_broker_position_once(pg_db, fake):
    fake.positions = [{"symbol": "ZZZZ", "qty": 5}]
    assert reconcile.reconcile(pg_db)["unknown_positions"] == 1
    assert reconcile.reconcile(pg_db)["unknown_positions"] == 0  # deduplicated
    (row,) = _audits(pg_db, "reconcile_unknown_position")
    assert row.payload["symbol"] == "ZZZZ"


def test_reconcile_marks_order_the_broker_has_never_heard_of(pg_db, fake):
    order = _order(pg_db, _intent(pg_db, _run(pg_db)), status="new", broker_id="ghost")
    pg_db.commit()
    fake.details["ghost"] = NotFound("order not found")
    counts = reconcile.reconcile(pg_db)
    pg_db.refresh(order)
    assert counts["orders_missing"] == 1 and order.status == "missing_at_broker"
    assert _audits(pg_db, "reconcile_order_missing")
    assert order_sync._orders_to_sync(pg_db) == []  # no longer polled


def test_reconcile_leaves_lagging_order_for_order_sync_to_journal(pg_db, fake):
    order = _order(pg_db, _intent(pg_db, _run(pg_db)), status="new", broker_id="lag")
    pg_db.commit()
    fake.details["lag"] = _details()  # filled at the broker; our row is stale
    counts = reconcile.reconcile(pg_db)
    pg_db.refresh(order)
    assert counts["orders_lagging"] == 1 and order.status == "new"
    order_sync.sync_orders(pg_db)  # the journal still gets the fill + outcome
    assert pg_db.scalars(select(TradeOutcome)).one().status == "open"


def test_reconcile_ignores_orders_the_broker_still_has_open(pg_db, fake):
    order = _order(pg_db, _intent(pg_db, _run(pg_db)), status="new", broker_id="live1")
    pg_db.commit()
    fake.open_orders = [{"broker_order_id": "live1", "symbol": "AAPL", "side": "buy"}]
    counts = reconcile.reconcile(pg_db)
    pg_db.refresh(order)
    assert counts["orders_missing"] == 0 and counts["orders_lagging"] == 0 and order.status == "new"


def test_reconcile_flags_exit_that_ended_unfilled_while_still_held(pg_db, fake, auto_gate):
    _open_trade(pg_db, fake)
    monitor._record_exit_safe(pg_db, "AAPL", {"broker_order_id": "sellr", "status": "accepted"},
                              "trailing_stop", 10)
    pg_db.commit()
    fake.details["sellr"] = _details(status="rejected", filled_qty=0, price=None, side="sell")
    order_sync.sync_orders(pg_db)
    fake.positions = [{"symbol": "AAPL", "qty": 10}]
    assert reconcile.reconcile(pg_db)["failed_exits"] == 1
    assert reconcile.reconcile(pg_db)["failed_exits"] == 0  # once
    assert pg_db.scalars(select(TradeOutcome)).one().status == "open"  # rejected sell closes nothing


def test_reconcile_changes_nothing_when_broker_is_unreadable(pg_db, fake):
    _i, _e, pm = _open_trade(pg_db, fake)
    fake.account_error = RuntimeError("503")
    counts = reconcile.reconcile(pg_db)
    pg_db.refresh(pm)
    assert counts["error"] and pm.is_open is True


def test_check_once_safe_runs_reconcile_in_its_own_failure_domain(monkeypatch):
    calls = []
    monkeypatch.setattr(monitor, "SessionLocal", lambda: SimpleNamespace(
        rollback=lambda: calls.append("rollback"), close=lambda: None))
    monkeypatch.setattr(monitor.order_sync, "sync_orders", lambda db: None)
    monkeypatch.setattr(monitor.reconcile, "reconcile", lambda db: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(monitor, "check_once", lambda db: calls.append("check_once") or {"skipped": True})
    monitor.check_once_safe()
    assert calls == ["rollback", "check_once"]  # reconcile failed, the cycle still ran


# --------------------------------------------------------------------------- #
# 3. kill switch cancels pending BUY entries only                             #
# --------------------------------------------------------------------------- #
def _alpaca_order(oid, side, status="new", symbol="AAPL"):
    return SimpleNamespace(id=oid, client_order_id=f"c{oid}", symbol=symbol, qty="5",
                           side=SimpleNamespace(value=side), status=SimpleNamespace(value=status))


def test_cancel_open_buy_orders_only_cancels_buys(monkeypatch):
    canceled = []
    orders = [_alpaca_order("b1", "buy"), _alpaca_order("s1", "sell"),
              _alpaca_order("stop1", "sell", status="held"), _alpaca_order("b2", "buy", "partially_filled"),
              _alpaca_order("b3", "buy", "pending_cancel")]
    client = SimpleNamespace(get_orders=lambda req: orders,
                             cancel_order_by_id=lambda oid: canceled.append(oid))
    monkeypatch.setattr(broker, "trading_client", lambda: client)
    result = broker.cancel_open_buy_orders()
    assert canceled == ["b1", "b2"]
    assert [o["broker_order_id"] for o in result["canceled"]] == ["b1", "b2"] and result["failed"] == []


def test_cancel_open_buy_orders_reports_failures_and_listing_errors(monkeypatch):
    def cancel(oid):
        if oid == "b1":
            raise RuntimeError("too late")
    client = SimpleNamespace(get_orders=lambda req: [_alpaca_order("b1", "buy"), _alpaca_order("b2", "buy")],
                             cancel_order_by_id=cancel)
    monkeypatch.setattr(broker, "trading_client", lambda: client)
    result = broker.cancel_open_buy_orders()
    assert [o["broker_order_id"] for o in result["canceled"]] == ["b2"]
    assert result["failed"][0]["broker_order_id"] == "b1" and "too late" in result["failed"][0]["error"]

    def boom(req):
        raise RuntimeError("down")
    monkeypatch.setattr(broker, "trading_client", lambda: SimpleNamespace(get_orders=boom))
    assert "could not list" in broker.cancel_open_buy_orders()["error"]


def test_kill_switch_engage_cancels_buys_audits_each_and_returns_result(pg_db, monkeypatch):
    order = _order(pg_db, _intent(pg_db, _run(pg_db)), broker_id="b1")
    pg_db.commit()
    monkeypatch.setattr(broker, "cancel_open_buy_orders", lambda: {
        "canceled": [{"broker_order_id": "b1", "symbol": "AAPL", "side": "buy", "status": "new", "qty": 10}],
        "failed": [{"broker_order_id": "b9", "symbol": "MSFT", "side": "buy", "status": "new",
                    "qty": 1, "error": "nope"}], "error": None})
    out = automation_router.kill_switch(KillSwitchRequest(engaged=True, reason="incident"), pg_db, "tester")
    assert out.kill_switch is True
    assert [o["broker_order_id"] for o in out.canceled_buy_orders] == ["b1"]
    assert out.cancel_failures[0]["error"] == "nope"
    (done,) = _audits(pg_db, "kill_switch_buy_canceled")
    assert done.order_id == order.id and done.intent_id == order.intent_id
    assert len(_audits(pg_db, "kill_switch_cancel_failed")) == 1
    assert _audits(pg_db, "kill_switch_engaged")


def test_kill_switch_stays_engaged_when_broker_is_down_and_release_cancels_nothing(pg_db, monkeypatch):
    def boom():
        raise RuntimeError("broker down")
    monkeypatch.setattr(broker, "cancel_open_buy_orders", boom)
    out = automation_router.kill_switch(KillSwitchRequest(engaged=True, reason="x"), pg_db, "tester")
    assert out.kill_switch is True and out.cancel_error and "broker down" in out.cancel_error
    monkeypatch.setattr(broker, "cancel_open_buy_orders", lambda: pytest.fail("release must not cancel"))
    out = automation_router.kill_switch(KillSwitchRequest(engaged=False), pg_db, "tester")
    assert out.kill_switch is False and out.canceled_buy_orders == []


# --------------------------------------------------------------------------- #
# 4. entry blocking when data / broker state is unavailable                   #
# --------------------------------------------------------------------------- #
@pytest.fixture()
def entry_env(monkeypatch, pg_db):
    gate = automation.evaluate_gate(mode="manual", kill_switch=False, capability_ok=True,
                                    capability_reason="ok")
    monkeypatch.setattr(automation, "order_gate", lambda db: gate)
    env = SimpleNamespace(
        quote={"last_price": 100.0, "quote_ts": datetime.now(UTC).isoformat(),
               "last_trade_ts": datetime.now(UTC).isoformat()},
        account_error=None, submitted=[], clock={"is_open": True},
    )

    def get_account():
        if env.account_error:
            raise env.account_error
        return {"equity": 100_000.0, "cash": 100_000.0, "buying_power": 100_000.0, "day_pnl_pct": 0.0,
                "is_paper": True, "is_margin": False}

    def submit(intent, qty, client_order_id):
        env.submitted.append(client_order_id)
        return {"broker_order_id": f"o-{client_order_id}", "client_order_id": client_order_id,
                "symbol": intent["ticker"], "side": intent["action"], "qty": qty, "order_type": "limit",
                "limit_price": intent["limit_price"], "stop_loss_price": 95.0, "take_profit_price": None,
                "status": "accepted", "raw": {}}

    monkeypatch.setattr(broker, "get_account", get_account)
    monkeypatch.setattr(broker, "get_positions", lambda: [])
    monkeypatch.setattr(broker, "get_quote_and_intraday",
                        lambda s: {"last_price": 100.0, "intraday_change_pct": 0.0})
    monkeypatch.setattr(broker, "submit_bracket_order", submit)
    monkeypatch.setattr(market_data, "fetch_snapshots",
                        lambda syms: {syms[0]: env.quote} if env.quote is not None else {})
    monkeypatch.setattr(market_data, "market_clock", lambda *a, **k: env.clock)
    return env


def _buy(db, limit=100.0):
    return _intent(db, _run(db), limit_price=limit, order_type="limit", position_size_percent=5,
                   stop_loss_percent=5, take_profit_percent=10, confidence=0.8)


def test_entry_proceeds_with_fresh_quote_and_near_market_limit(pg_db, entry_env):
    intent = _buy(pg_db)
    order = execution.execute_intent(pg_db, intent, "user:operator")
    assert intent.status == IntentStatus.submitted and order.client_order_id == entry_env.submitted[0]


@pytest.mark.parametrize("case,code", [
    ("stale", "STALE_QUOTE"), ("no_quote", "NO_QUOTE"), ("no_price", "NO_MARKET_PRICE"),
    ("off_market", "LIMIT_PRICE_OFF_MARKET"), ("data_error", "MARKET_DATA_UNAVAILABLE"),
    ("broker_error", "BROKER_UNAVAILABLE"),
])
def test_buy_is_blocked_fail_closed(pg_db, entry_env, monkeypatch, case, code):
    intent = _buy(pg_db, limit=130.0 if case == "off_market" else 100.0)
    old = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    if case == "stale":
        entry_env.quote = {"last_price": 100.0, "quote_ts": old, "last_trade_ts": old}
    elif case == "no_quote":
        entry_env.quote = None
    elif case == "no_price":
        entry_env.quote = {"last_price": 0, "quote_ts": old}
    elif case == "data_error":
        monkeypatch.setattr(market_data, "fetch_snapshots",
                            lambda s: (_ for _ in ()).throw(RuntimeError("feed down")))
    elif case == "broker_error":
        entry_env.account_error = RuntimeError("alpaca 503")
    with pytest.raises(execution.ExecutionError) as e:
        execution.execute_intent(pg_db, intent, "user:operator")
    assert code in str(e.value.detail) and entry_env.submitted == []
    assert intent.status == IntentStatus.pending_approval  # left for a retry
    assert _audits(pg_db, "entry_blocked")[-1].payload["code"] == code


def test_stale_quote_is_ignored_while_the_market_is_closed(pg_db, entry_env):
    old = (datetime.now(UTC) - timedelta(hours=14)).isoformat()
    entry_env.quote = {"last_price": 100.0, "quote_ts": old, "last_trade_ts": old}
    entry_env.clock = {"is_open": False}
    assert execution.execute_intent(pg_db, _buy(pg_db), "user:operator").status == "accepted"


def test_sells_still_work_when_quote_data_and_snapshot_are_down(pg_db, entry_env, monkeypatch):
    monkeypatch.setattr(market_data, "fetch_snapshots",
                        lambda s: pytest.fail("a sell must not depend on the entry data guard"))
    monkeypatch.setattr(broker, "get_positions", lambda: [{"symbol": "AAPL", "qty": 10}])
    monkeypatch.setattr(broker, "get_position_qty", lambda s: 10)
    monkeypatch.setattr(broker, "get_available_qty", lambda s: 10)
    intent = _intent(pg_db, _run(pg_db), action=IntentAction.sell, limit_price=100.0, order_type="limit",
                     position_size_percent=5, stop_loss_percent=5, take_profit_percent=10)
    order = execution.execute_intent(pg_db, intent, "user:operator")
    assert order.side == "sell" and intent.status == IntentStatus.submitted


def test_duplicate_approval_cannot_double_submit(pg_db, entry_env):
    intent = _buy(pg_db)
    first = execution.execute_intent(pg_db, intent, "user:operator")
    pg_db.commit()
    with pytest.raises(execution.ExecutionError) as e:
        execution.execute_intent(pg_db, intent, "user:operator")  # a repeated request
    assert e.value.status_code == 409
    assert len(entry_env.submitted) == 1 and first.client_order_id == entry_env.submitted[0]
    assert _audits(pg_db, "duplicate_submit_blocked")
    assert len(pg_db.scalars(select(Order).where(Order.intent_id == intent.id)).all()) == 1


def test_client_order_id_is_stable_per_intent(pg_db, entry_env):
    intent = _buy(pg_db)
    execution.execute_intent(pg_db, intent, "user:operator")
    assert entry_env.submitted[0] == f"intent-{intent.id}-{int(intent.created_at.timestamp())}"


# --------------------------------------------------------------------------- #
# 5. rejected / partially filled orders leave consistent state                #
# --------------------------------------------------------------------------- #
def test_rejected_entry_opens_no_outcome(pg_db, fake):
    order = _order(pg_db, _intent(pg_db, _run(pg_db)), status="new", broker_id="rej")
    pg_db.commit()
    fake.details["rej"] = _details(status="rejected", filled_qty=0, price=None)
    order_sync.sync_orders(pg_db)
    pg_db.refresh(order)
    assert order.status == "rejected" and pg_db.scalars(select(TradeOutcome)).all() == []


def test_partially_filled_full_exit_closes_only_when_filled(pg_db, fake, auto_gate):
    _open_trade(pg_db, fake)
    monitor._record_exit_safe(pg_db, "AAPL", {"broker_order_id": "sellp", "status": "accepted"},
                              "trailing_stop", 10)
    pg_db.commit()
    fake.details["sellp"] = _details(status="partially_filled", filled_qty=4, price=90.0, side="sell")
    order_sync.sync_orders(pg_db)
    assert pg_db.scalars(select(TradeOutcome)).one().status == "open"
    fake.details["sellp"] = _details(status="filled", filled_qty=10, price=90.0, side="sell",
                                     filled_at=T0 + timedelta(days=1))
    order_sync.sync_orders(pg_db)
    assert pg_db.scalars(select(TradeOutcome)).one().status == "closed"
