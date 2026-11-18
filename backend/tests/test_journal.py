"""Trade journal: order sync, trade outcomes, candidate outcomes and the API.
DB tests use the `pg_db` fixture; the broker is always a fake (no network, no orders)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import (
    AnalysisRun,
    CandidateOutcome,
    IntentAction,
    IntentStatus,
    MarketBar,
    Order,
    OrderFill,
    TradeIntent,
    TradeOutcome,
)
from app.services import broker, order_sync, outcomes

UTC = timezone.utc
T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)  # a Monday


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #
def _run(db) -> AnalysisRun:
    run = AnalysisRun(model="m", input_context={}, raw_response={})
    db.add(run)
    db.flush()
    return run


def _intent(db, run, ticker="AAPL", action=IntentAction.buy, status=IntentStatus.pending_approval,
            created_at=T0) -> TradeIntent:
    i = TradeIntent(run_id=run.id, ticker=ticker, action=action, status=status, created_at=created_at)
    db.add(i)
    db.flush()
    return i


def _order(db, intent, *, side="buy", status="new", broker_id="b1", qty=10, symbol="AAPL",
           exit_reason=None) -> Order:
    o = Order(intent_id=intent.id, broker_order_id=broker_id, client_order_id=f"c-{broker_id}",
              symbol=symbol, side=side, qty=qty, order_type="limit", status=status, is_live=False,
              exit_reason=exit_reason)
    db.add(o)
    db.flush()
    return o


def _bars(db, symbol, closes, start=T0):
    for n, close in enumerate(closes):
        ts = (start - timedelta(hours=11)) + timedelta(days=n)
        db.add(MarketBar(symbol=symbol, timeframe="1Day", ts=ts,
                         open=close, high=close, low=close, close=close, volume=1, feed="iex"))
    db.flush()


def _details(status="filled", filled_qty=10, price=100.0, filled_at=T0, side="buy", legs=None,
             order_type="limit"):
    return {"status": status, "filled_qty": filled_qty, "filled_avg_price": price,
            "filled_at": filled_at, "side": side, "order_type": order_type, "legs": legs or []}


@pytest.fixture()
def fake_broker(monkeypatch):
    state = {"details": {}, "calls": []}

    def get_order_details(broker_order_id):
        state["calls"].append(broker_order_id)
        d = state["details"][broker_order_id]
        if isinstance(d, Exception):
            raise d
        return d

    monkeypatch.setattr(broker, "get_order_details", get_order_details)
    # Any write-path call fails the test.
    for name in ("submit_bracket_order", "cancel_open_orders", "place_stop_order", "close_position",
                 "close_all_positions"):
        monkeypatch.setattr(broker, name, lambda *a, **k: pytest.fail("order call attempted"))
    return state


# --------------------------------------------------------------------------- #
# broker.get_order_details (read-only wrapper)                                #
# --------------------------------------------------------------------------- #
def test_get_order_details_maps_order_and_legs(monkeypatch):
    def order(**kw):
        base = dict(id="x", status=SimpleNamespace(value="filled"), side=SimpleNamespace(value="buy"),
                    order_type=SimpleNamespace(value="limit"), qty="10", filled_qty="10",
                    filled_avg_price="101.5", filled_at=T0, legs=None)
        base.update(kw)
        return SimpleNamespace(**base)

    leg = order(id="l1", side=SimpleNamespace(value="sell"), order_type=SimpleNamespace(value="stop"),
                status=SimpleNamespace(value="new"), filled_qty="0", filled_avg_price=None, filled_at=None)
    client = SimpleNamespace(get_order_by_id=lambda oid: order(legs=[leg]))
    monkeypatch.setattr(broker, "trading_client", lambda: client)

    d = broker.get_order_details("x")
    assert d["status"] == "filled" and d["filled_qty"] == 10 and d["filled_avg_price"] == 101.5
    assert d["filled_at"] == T0
    assert d["legs"][0]["order_type"] == "stop" and d["legs"][0]["filled_avg_price"] is None


# --------------------------------------------------------------------------- #
# order sync                                                                  #
# --------------------------------------------------------------------------- #
def test_buy_fill_opens_outcome_and_repeat_sync_is_deduped(pg_db, fake_broker):
    intent = _intent(pg_db, _run(pg_db))
    order = _order(pg_db, intent, status="new")
    pg_db.commit()
    fake_broker["details"]["b1"] = _details(status="partially_filled", filled_qty=4)

    r1 = order_sync.sync_orders(pg_db)
    r2 = order_sync.sync_orders(pg_db)  # same state again
    assert r1["fills_recorded"] == 1 and r2["fills_recorded"] == 0
    assert pg_db.scalars(select(TradeOutcome)).all() == []

    fake_broker["details"]["b1"] = _details()
    order_sync.sync_orders(pg_db)
    order_sync.sync_orders(pg_db)

    pg_db.refresh(order)
    assert order.status == "filled" and order.filled_qty == 10 and float(order.filled_avg_price) == 100.0
    assert len(pg_db.scalars(select(OrderFill)).all()) == 2
    out = pg_db.scalars(select(TradeOutcome)).one()
    assert out.status == "open" and out.entry_order_id == order.id and out.qty == 10
    assert float(out.entry_price) == 100.0 and out.intent_id == intent.id


def test_sell_fill_closes_outcome_with_pnl_holding_days_and_spy(pg_db, fake_broker):
    run = _run(pg_db)
    buy_intent, sell_intent = _intent(pg_db, run), _intent(pg_db, run, action=IntentAction.sell)
    _order(pg_db, buy_intent, status="new", broker_id="buy1")
    _order(pg_db, sell_intent, side="sell", status="new", broker_id="sell1", exit_reason="take_profit")
    _bars(pg_db, "SPY", [400, 404, 408, 412])
    pg_db.commit()
    fake_broker["details"]["buy1"] = _details(price=100.0, filled_at=T0)
    order_sync.sync_orders(pg_db)

    exit_at = T0 + timedelta(days=2, hours=1)
    fake_broker["details"]["sell1"] = _details(price=110.0, filled_at=exit_at, side="sell")
    order_sync.sync_orders(pg_db)

    out = pg_db.scalars(select(TradeOutcome)).one()
    assert out.status == "closed" and out.exit_reason == "take_profit"
    assert float(out.exit_price) == 110.0 and float(out.realized_pnl) == 100.0
    assert float(out.realized_pnl_pct) == 10.0
    assert out.holding_days == pytest.approx(2.0417, abs=1e-3)
    assert out.exit_order_id is not None and out.benchmark_symbol == "SPY"
    assert float(out.benchmark_return_pct) == 2.0  # 400 -> 408


def test_bracket_exit_leg_closes_outcome_as_stop_loss(pg_db, fake_broker):
    _order(pg_db, _intent(pg_db, _run(pg_db)), status="new")
    pg_db.commit()
    fake_broker["details"]["b1"] = _details()
    order_sync.sync_orders(pg_db)  # filled parent stays tracked while its exit is open
    assert order_sync._orders_to_sync(pg_db)

    leg = _details(price=95.0, filled_at=T0 + timedelta(days=1), side="sell", order_type="stop")
    fake_broker["details"]["b1"] = _details(legs=[leg])
    order_sync.sync_orders(pg_db)

    out = pg_db.scalars(select(TradeOutcome)).one()
    assert out.status == "closed" and out.exit_reason == "stop_loss"
    assert float(out.realized_pnl) == -50.0 and out.exit_order_id is None
    assert order_sync._orders_to_sync(pg_db) == []  # nothing left to poll
    assert out.benchmark_return_pct is None  # no SPY bars stored


def test_terminal_orders_are_not_polled_and_broker_errors_are_isolated(pg_db, fake_broker):
    intent = _intent(pg_db, _run(pg_db))
    _order(pg_db, intent, status="canceled", broker_id="done")
    _order(pg_db, intent, status="new", broker_id="boom")
    _order(pg_db, intent, status="new", broker_id="ok")
    pg_db.commit()
    fake_broker["details"]["boom"] = RuntimeError("broker down")
    fake_broker["details"]["ok"] = _details(status="canceled", filled_qty=0, price=None, filled_at=None)

    result = order_sync.sync_orders(pg_db)

    assert result["errors"] == 1 and result["checked"] == 2
    assert "done" not in fake_broker["calls"]
    assert pg_db.scalars(select(Order).where(Order.broker_order_id == "ok")).one().status == "canceled"
    assert pg_db.scalars(select(TradeOutcome)).all() == []


def test_check_once_safe_syncs_orders_even_if_check_once_skips(monkeypatch):
    from app.services import monitor

    calls = []
    monkeypatch.setattr(monitor.order_sync, "sync_orders", lambda db: calls.append("sync"))
    monkeypatch.setattr(monitor, "check_once", lambda db: calls.append("check") or {"skipped": True})
    monkeypatch.setattr(monitor, "SessionLocal", lambda: SimpleNamespace(close=lambda: None,
                                                                         rollback=lambda: None))
    monitor.check_once_safe()
    assert calls == ["sync", "check"]


def test_check_once_safe_survives_sync_failure(monkeypatch):
    from app.services import monitor

    calls = []

    def bad_sync(db):
        raise RuntimeError("sync broke")

    monkeypatch.setattr(monitor.order_sync, "sync_orders", bad_sync)
    monkeypatch.setattr(monitor, "check_once", lambda db: calls.append("check") or {"skipped": True})
    monkeypatch.setattr(monitor, "SessionLocal", lambda: SimpleNamespace(close=lambda: None,
                                                                         rollback=lambda: None))
    monitor.check_once_safe()
    assert calls == ["check"]


# --------------------------------------------------------------------------- #
# candidate outcomes                                                          #
# --------------------------------------------------------------------------- #
def test_candidate_outcomes_forward_returns_and_skip_insufficient_bars(pg_db):
    run = _run(pg_db)
    rejected = _intent(pg_db, run, status=IntentStatus.rejected_by_risk)
    watch = _intent(pg_db, run, ticker="MSFT", action=IntentAction.watch,
                    status=IntentStatus.recommended)
    # Intent made at 15:00 on day 0; bars start that day. AAPL has 7 bars (horizons 1 and 5).
    _bars(pg_db, "AAPL", [100, 101, 102, 103, 104, 110, 120])
    _bars(pg_db, "SPY", [400, 400, 400, 400, 400, 420, 440])
    pg_db.commit()

    result = outcomes.compute_candidate_outcomes(pg_db)
    again = outcomes.compute_candidate_outcomes(pg_db)

    rows = {(r.intent_id, r.horizon_days): r for r in pg_db.scalars(select(CandidateOutcome)).all()}
    assert set(rows) == {(rejected.id, 1), (rejected.id, 5)}  # MSFT has no bars; 20 needs 21 bars
    assert rows[(rejected.id, 1)].return_pct == pytest.approx(1.0)
    assert rows[(rejected.id, 5)].return_pct == pytest.approx(10.0)
    assert rows[(rejected.id, 5)].benchmark_return_pct == pytest.approx(5.0)
    assert rows[(rejected.id, 5)].rule_version == "fwd-close-v1"
    assert result["written"] == 2 and again["written"] == 0
    assert watch.id not in {i for i, _ in rows}


def test_candidate_outcome_start_is_first_bar_on_or_after_intent_date(pg_db):
    run = _run(pg_db)
    # Intent created Saturday: first bar on/after is Monday.
    sat = datetime(2026, 2, 28, 12, 0, tzinfo=UTC)
    i = _intent(pg_db, run, created_at=sat)
    _bars(pg_db, "AAPL", [50], start=sat - timedelta(days=1))   # Friday bar, before the intent
    _bars(pg_db, "AAPL", [200, 210], start=sat + timedelta(days=2))  # Monday, Tuesday
    pg_db.commit()
    outcomes.compute_candidate_outcomes(pg_db)
    row = pg_db.scalars(select(CandidateOutcome).where(CandidateOutcome.intent_id == i.id)).one()
    assert row.start_price == 200 and row.end_price == 210 and row.benchmark_return_pct is None


# --------------------------------------------------------------------------- #
# API                                                                         #
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(pg_db, fake_broker):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.deps import require_token
    from app.main import app

    app.dependency_overrides[get_db] = lambda: pg_db
    app.dependency_overrides[require_token] = lambda: "user:test"
    try:
        yield TestClient(app)  # no `with`: startup (schema check, monitor loop) is not run
    finally:
        app.dependency_overrides.clear()


def test_journal_endpoints(client, pg_db, fake_broker):
    run = _run(pg_db)
    intent = _intent(pg_db, run, status=IntentStatus.submitted)
    other = _intent(pg_db, run, ticker="MSFT", status=IntentStatus.rejected_by_risk)
    _order(pg_db, intent, status="new")
    _bars(pg_db, "AAPL", [100, 102])
    _bars(pg_db, "MSFT", [100, 90])
    _bars(pg_db, "SPY", [400, 404])
    pg_db.commit()
    fake_broker["details"]["b1"] = _details()

    resp = client.post("/journal/sync")
    assert resp.status_code == 200
    body = resp.json()
    assert body["orders"]["checked"] == 1 and body["candidates"]["written"] == 2

    trades = client.get("/journal/trades").json()
    assert len(trades) == 1
    assert trades[0]["intent_id"] == intent.id and trades[0]["run_id"] == run.id
    assert trades[0]["status"] == "open" and trades[0]["symbol"] == "AAPL"
    assert "market_snapshot_id" in trades[0]

    cands = client.get("/journal/candidates").json()
    by_status = {c["status"]: c for c in cands}
    assert by_status["submitted"]["avg_return_pct"] == pytest.approx(2.0)
    assert by_status["rejected_by_risk"]["avg_return_pct"] == pytest.approx(-10.0)
    assert by_status["rejected_by_risk"]["avg_excess_return_pct"] == pytest.approx(-11.0)
    assert by_status["rejected_by_risk"]["hit_rate"] == 0.0
    assert other.id


def test_journal_requires_auth():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    assert c.get("/journal/trades").status_code == 401
    assert c.post("/journal/sync").status_code == 401


# --------------------------------------------------------------------------- #
# provenance                                                                  #
# --------------------------------------------------------------------------- #
def test_trade_intent_provenance_columns_roundtrip(pg_db):
    run = _run(pg_db)
    i = _intent(pg_db, run)
    i.strategy_id, i.strategy_version, i.prompt_version = "claude-analysis", "pv1", "pv1"
    i.evidence = ["rsi=71"]
    i.expires_at = T0 + timedelta(minutes=60)
    pg_db.commit()
    pg_db.refresh(i)
    assert i.evidence == ["rsi=71"] and i.strategy_id == "claude-analysis" and i.market_snapshot_id is None
