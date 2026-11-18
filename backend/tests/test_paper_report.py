"""Paper experiment report: modelled vs actual fills. Read-only; the broker is never touched."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.backtest import paper_report, report_io
from app.backtest.data import BarStore, make_bar
from app.backtest.fills import CostModel
from app.services import broker

COST = CostModel(slippage_bps=5, fee_bps=0, fee_per_share=0, max_volume_pct=100)


@pytest.fixture(autouse=True)
def no_broker(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the paper report must not call the broker")
    for name in dir(broker):
        if name.startswith(("submit", "cancel", "place", "close", "get_")):
            monkeypatch.setattr(broker, name, boom)


def store():
    days = [date(2026, 3, 2) + timedelta(days=i) for i in range(5)]
    return BarStore({"AAPL": [make_bar(d, 100, 102, 99, 101) for d in days]})


def order(oid, **over):
    base = {"id": oid, "symbol": "AAPL", "side": "buy", "qty": 10, "order_type": "limit",
            "limit_price": 100.0, "submitted_at": datetime(2026, 3, 2, 15, 0, tzinfo=timezone.utc),
            "status": "filled", "filled_qty": 10, "filled_avg_price": 99.9,
            "fills": [(datetime(2026, 3, 2, 15, 0, 42, tzinfo=timezone.utc), 10)]}
    base.update(over)
    return base


def test_filled_limit_order_compares_price_slippage_and_latency():
    (row,) = paper_report.build_paper_report([order(1)], store(), COST)["orders"]
    assert row["prediction"]["filled"] is True
    assert row["prediction"]["price"] == 100.0           # min(open, limit)*(1+5bps) capped at the limit
    assert row["actual"]["latency_sec"] == 42.0
    assert row["actual"]["slippage_bps"] == pytest.approx(-10.0, abs=0.1)
    assert row["price_error_bps"] == pytest.approx(-10.0, abs=0.1)
    assert row["model_bar_date"] == "2026-03-02"


def test_aggregate_fill_rate_difference_and_slippage_bias():
    orders = [
        order(1),                                                        # both fill
        order(2, limit_price=98.0, status="expired", filled_qty=0,       # neither (low 99 > 98)
              filled_avg_price=None, fills=[]),
        order(3, limit_price=98.0, filled_avg_price=98.0),               # actual only
        order(4, status="canceled", filled_qty=0, filled_avg_price=None, fills=[]),  # model only
    ]
    agg = paper_report.build_paper_report(orders, store(), COST)["aggregate"]
    assert agg["orders"] == 4 and agg["comparable_orders"] == 4
    assert agg["actual_fill_rate"] == 0.5 and agg["predicted_fill_rate"] == 0.5
    assert agg["fill_rate_diff"] == 0.0
    assert agg["agreement"] == {"both_filled": 1, "model_only_filled": 1, "actual_only_filled": 1,
                                "neither_filled": 1}
    assert agg["slippage"]["n"] == 1 and agg["slippage"]["bias_bps"] == pytest.approx(-10.0, abs=0.1)
    assert agg["latency_sec"]["n"] == 2


def test_market_sell_and_unsupported_and_missing_bars():
    mkt = order(5, side="sell", order_type="market", limit_price=None, filled_avg_price=100.2)
    stop = order(6, order_type="stop", limit_price=None)
    nobars = order(7, symbol="ZZZ")
    rep = paper_report.build_paper_report([mkt, stop, nobars], store(), COST)
    rows = {r["order_id"]: r for r in rep["orders"]}
    assert rows[5]["prediction"]["price"] == pytest.approx(100 * (1 - 0.0005), abs=1e-4)
    assert rows[5]["actual"]["slippage_bps"] == pytest.approx(-20.0, abs=0.1)  # sold above the open
    assert rows[6]["prediction"]["status"] == "unsupported_order_type"
    assert rows[7]["prediction"]["status"] == "no_bars"
    assert rep["aggregate"]["comparable_orders"] == 1 and rep["aggregate"]["not_comparable"] == 2


def test_empty_report_is_valid():
    agg = paper_report.build_paper_report([], BarStore({}), COST)["aggregate"]
    assert agg["orders"] == 0 and agg["fill_rate_diff"] is None


def test_report_is_deterministic():
    a = report_io.dumps(paper_report.build_paper_report([order(2), order(1)], store(), COST))
    b = report_io.dumps(paper_report.build_paper_report([order(1), order(2)], store(), COST))
    assert a == b


def test_cli_reads_orders_from_db_only(pg_db, tmp_path):
    from app.models import AnalysisRun, IntentAction, MarketBar, Order, OrderFill, TradeIntent

    for b in store()._bars["AAPL"]:
        pg_db.add(MarketBar(symbol="AAPL", timeframe="1Day", ts=b.timestamp, open=b.open, high=b.high,
                            low=b.low, close=b.close, volume=b.volume, feed="iex"))
    run = AnalysisRun(model="m", input_context={}, raw_response={})
    pg_db.add(run)
    pg_db.flush()
    intent = TradeIntent(run_id=run.id, ticker="AAPL", action=IntentAction.buy)
    pg_db.add(intent)
    pg_db.flush()
    sub = datetime(2026, 3, 2, 15, 0, tzinfo=timezone.utc)
    paper = Order(intent_id=intent.id, client_order_id="c1", symbol="AAPL", side="buy", qty=10,
                  order_type="limit", limit_price=100, status="filled", filled_qty=10,
                  filled_avg_price=99.9, is_live=False, submitted_at=sub)
    live = Order(intent_id=intent.id, client_order_id="c2", symbol="AAPL", side="buy", qty=10,
                 order_type="limit", limit_price=100, status="filled", filled_qty=10,
                 filled_avg_price=99.9, is_live=True, submitted_at=sub)
    pg_db.add_all([paper, live])
    pg_db.flush()
    pg_db.add(OrderFill(order_id=paper.id, broker_order_id="b1", status="filled", requested_qty=10,
                        filled_qty=10, filled_avg_price=99.9, filled_at=sub + timedelta(seconds=30)))
    pg_db.commit()
    out = tmp_path / "paper.json"
    argv = ["--start", "2026-03-01", "--end", "2026-03-31", "--created-at", "2026-04-01T00:00:00Z",
            "--out", str(out)]
    assert paper_report.main(argv, session=pg_db) == 0
    rep = json.loads(out.read_text())
    assert rep["kind"] == "paper_report" and rep["summary"]["orders"] == 1  # live orders excluded
    assert rep["orders"][0]["actual"]["latency_sec"] == 30.0
    first = out.read_text()
    assert paper_report.main(argv, session=pg_db) == 0
    assert out.read_text() == first
