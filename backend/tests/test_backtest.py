"""Backtest engine tests: pure logic on synthetic bars; the CLI test uses the pg_db fixture."""
import json
import math
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from app.backtest import metrics
from app.backtest.data import Bar, BarStore, LeakageError, make_bar
from app.backtest.engine import BacktestConfig, buy_and_hold, run_backtest
from app.backtest.fills import (
    CostModel,
    evaluate_exit,
    fill_buy_limit,
    fill_sell_limit,
)
from app.backtest.run import main as cli_main
from app.backtest.runner import run_experiment
from app.backtest.splits import chronological_split, walk_forward_windows
from app.backtest.strategies import ReplayStrategy, Signal

NO_COST = CostModel(slippage_bps=0, fee_bps=0, fee_per_share=0, max_volume_pct=100)


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def random_store(symbols, n=400, seed=7, start=date(2024, 1, 1)) -> tuple[BarStore, list[date]]:
    rng = random.Random(seed)
    days = weekdays(start, n)
    data = {}
    for sym in symbols:
        px, bars = 100.0, []
        drift = rng.uniform(-0.0003, 0.0010)
        for d in days:
            o = px * (1 + rng.uniform(-0.005, 0.005))
            c = o * (1 + drift + rng.uniform(-0.015, 0.015))
            bars.append(make_bar(d, o, max(o, c) * 1.004, min(o, c) * 0.996, c, rng.uniform(5e5, 2e6)))
            px = c
        data[sym] = bars
    return BarStore(data), days


def flat_store(symbols, days):
    return BarStore({s: [make_bar(d, 100, 101, 99, 100) for d in days] for s in symbols})


# ---- leakage -------------------------------------------------------------------------------

def test_view_contains_no_future_bars_and_is_immutable():
    store, days = random_store(["AAA"], n=30)
    view = store.view(days[10])
    assert view.bars("AAA")[-1].date == days[10]
    assert len(view.bars("AAA")) == 11
    with pytest.raises(LeakageError):
        view.bars("AAA", end=days[11])
    with pytest.raises(LeakageError):
        view.bar_on("AAA", days[11])
    with pytest.raises(AttributeError):
        view.as_of = days[20]


class PeekingStrategy:
    name = "peek"

    def on_close(self, view, ctx):
        view.bars("AAA", end=view.as_of + timedelta(days=1))
        return []


def test_strategy_reading_t_plus_one_fails():
    store, days = random_store(["AAA", "SPY"], n=30)
    with pytest.raises(LeakageError):
        run_backtest(store, PeekingStrategy(), ["AAA"], days, BacktestConfig())


# ---- fill model ----------------------------------------------------------------------------

def test_buy_limit_gap_below_limit_fills_at_open():
    bar = make_bar(date(2025, 1, 2), 95, 97, 94, 96)
    f = fill_buy_limit(100, 10, bar, NO_COST)
    assert f.price == 95 and f.qty == 10


def test_buy_limit_gap_above_limit_does_not_fill():
    bar = make_bar(date(2025, 1, 2), 105, 108, 104, 106)
    assert fill_buy_limit(100, 10, bar, NO_COST) is None


def test_buy_limit_touched_intrabar_fills_at_limit_and_slippage_is_capped():
    bar = make_bar(date(2025, 1, 2), 102, 103, 99, 101)
    cost = CostModel(slippage_bps=50, max_volume_pct=100)
    f = fill_buy_limit(100, 10, bar, cost)
    assert f.price == 100  # open above limit -> limit; slippage cannot push past the limit
    bar2 = make_bar(date(2025, 1, 2), 90, 103, 89, 101)
    assert fill_buy_limit(100, 10, bar2, cost).price == pytest.approx(90 * 1.005)


def test_sell_limit_is_symmetric():
    bar = make_bar(date(2025, 1, 2), 112, 115, 110, 113)
    assert fill_sell_limit(110, 5, bar, NO_COST).price == 112          # gap above the limit
    assert fill_sell_limit(120, 5, bar, NO_COST) is None
    cost = CostModel(slippage_bps=100, max_volume_pct=100)
    assert fill_sell_limit(110, 5, make_bar(date(2025, 1, 2), 109, 111, 108, 110), cost).price == 110


def test_stop_and_target_in_same_bar_assumes_stop():
    bar = make_bar(date(2025, 1, 2), 100, 112, 94, 100)
    f = evaluate_exit(bar, stop=95, target=110, qty=10, cost=NO_COST)
    assert f.reason == "stop_loss" and f.price == 95


def test_gap_through_stop_fills_at_open_and_gap_over_target_at_open():
    down = make_bar(date(2025, 1, 2), 90, 91, 88, 89)
    assert evaluate_exit(down, 95, 110, 10, NO_COST).price == 90
    up = make_bar(date(2025, 1, 2), 115, 118, 114, 116)
    f = evaluate_exit(up, 95, 110, 10, NO_COST)
    assert f.reason == "take_profit" and f.price == 115


def test_no_target_on_entry_bar():
    bar = make_bar(date(2025, 1, 2), 100, 120, 99, 100)
    assert evaluate_exit(bar, 95, 110, 10, NO_COST, allow_target=False) is None


def test_partial_fill_capped_by_volume_and_fees():
    bar = make_bar(date(2025, 1, 2), 100, 101, 99, 100, v=1000)
    cost = CostModel(slippage_bps=0, fee_bps=10, fee_per_share=0.01, max_volume_pct=10)
    f = fill_buy_limit(101, 500, bar, cost)
    assert f.qty == 100
    assert f.fee == pytest.approx(100 * 100 * 0.001 + 100 * 0.01)
    assert fill_buy_limit(101, 500, make_bar(date(2025, 1, 2), 100, 101, 99, 100, v=5), cost) is None


# ---- engine / risk sizing ------------------------------------------------------------------

def test_entries_sized_through_risk_engine():
    days = weekdays(date(2025, 1, 2), 12)
    syms = ["A", "B", "C", "D", "E"]
    store = flat_store(syms, days)
    sigs = {days[0]: [Signal(s, stop_loss_pct=5, max_hold_days=3) for s in syms]}
    cfg = BacktestConfig(slippage_bps=0, daily_loss_limit_pct=10)
    res = run_backtest(store, ReplayStrategy(sigs), syms, days, cfg)
    assert len(res.trades) == 3                                  # max 3 open positions
    assert res.rejections.get("TOO_MANY_POSITIONS") == 2
    for t in res.trades:
        assert t.qty * 100.5 <= 0.10 * 100_000                   # max 10% of equity at the limit
        assert t.reason == "max_hold"
        assert t.entry_date == days[1]                           # filled the bar AFTER the signal


def test_daily_loss_halt_blocks_entries():
    days = weekdays(date(2025, 1, 2), 6)
    bars = [make_bar(days[0], 100, 101, 99, 100)]
    bars += [make_bar(d, 100, 101, 99, 100) for d in days[1:]]
    store = BarStore({"A": bars})
    sigs = {days[1]: [Signal("A", 5)]}
    # a 0% loss limit halts every day (day P&L 0 <= -0)
    res = run_backtest(store, ReplayStrategy(sigs), ["A"], days, BacktestConfig(daily_loss_limit_pct=0))
    assert not res.trades and res.rejections.get("DAILY_LOSS_HALT") == 1


def test_stop_loss_exit_in_engine():
    days = weekdays(date(2025, 1, 2), 6)
    bars = [make_bar(d, 100, 101, 99, 100) for d in days[:3]]
    bars.append(make_bar(days[3], 100, 100, 90, 92))
    bars += [make_bar(d, 92, 93, 91, 92) for d in days[4:]]
    store = BarStore({"A": bars})
    res = run_backtest(store, ReplayStrategy({days[0]: [Signal("A", 5, 20)]}), ["A"], days,
                       BacktestConfig(slippage_bps=0))
    t = res.trades[0]
    assert t.reason == "stop_loss" and t.exit_date == days[3]
    assert t.exit_price == pytest.approx(100 * 0.95)  # entry at open 100
    assert t.pnl < 0


def test_buy_and_hold_benchmark():
    days = weekdays(date(2025, 1, 2), 3)
    store = BarStore({"SPY": [make_bar(days[0], 100, 101, 99, 100), make_bar(days[1], 100, 111, 99, 110),
                              make_bar(days[2], 110, 121, 109, 120)]})
    cfg = BacktestConfig(slippage_bps=0, initial_cash=1000)
    curve = buy_and_hold(store, "SPY", days, cfg)
    assert [v for _, v in curve] == [1000, 1100, 1200]


# ---- metrics -------------------------------------------------------------------------------

def test_metrics_hand_computed():
    ds = weekdays(date(2025, 1, 6), 4)
    curve = list(zip(ds, [100.0, 110.0, 99.0, 108.9]))
    assert metrics.total_return(curve) == pytest.approx(0.089)
    assert metrics.max_drawdown(curve) == pytest.approx(0.1)
    rets = metrics.daily_returns(curve)
    assert rets == pytest.approx([0.1, -0.1, 0.1])
    sd = math.sqrt(0.0266666667 / 2)
    assert metrics.volatility(rets) == pytest.approx(sd * math.sqrt(252), rel=1e-6)
    assert metrics.sharpe(rets) == pytest.approx((0.1 / 3) / sd * math.sqrt(252), rel=1e-6)
    stats = metrics.trade_stats([10, -5, 20, -10])
    assert stats["win_rate"] == 0.5 and stats["avg_win_pct"] == 15 and stats["avg_loss_pct"] == -7.5
    assert stats["expectancy_pct"] == pytest.approx(3.75)
    two_years = [(date(2024, 1, 1), 100.0), (date(2026, 1, 1), 121.0)]
    assert metrics.cagr(two_years) == pytest.approx(0.1, abs=2e-3)


def test_bootstrap_is_seeded_and_brackets_mean():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0, -1.0, 0.5]
    a = metrics.bootstrap_ci(vals, 500, seed=3)
    assert a == metrics.bootstrap_ci(vals, 500, seed=3)
    assert a[0] <= sum(vals) / len(vals) <= a[1]
    assert metrics.bootstrap_ci([]) is None


# ---- splits --------------------------------------------------------------------------------

def test_chronological_split_non_overlapping_and_ordered():
    days = weekdays(date(2024, 1, 1), 100)
    w = chronological_split(days)
    order = [w["development"], w["validation"], w["holdout"]]
    assert order[0].start == days[0] and order[-1].end == days[-1]
    assert sum(x.days for x in order) == 100
    for a, b in zip(order, order[1:]):
        assert a.end < b.start
    with pytest.raises(ValueError):
        chronological_split(days[:2])


def test_walk_forward_windows():
    days = weekdays(date(2024, 1, 1), 100)
    folds = walk_forward_windows(days, 30, 10)
    assert len(folds) == 7
    for train, test in folds:
        assert train.end < test.start
    tests = [t for _, t in folds]
    for a, b in zip(tests, tests[1:]):
        assert a.end < b.start


# ---- reproducibility / experiments ---------------------------------------------------------

def test_same_input_same_report():
    store, days = random_store(["AAA", "BBB", "CCC", "SPY", "XLK", "XLF", "XLE"])
    cfg = BacktestConfig(bootstrap_samples=200)
    args = (store, ["AAA", "BBB", "CCC"], days[250], days[-1], cfg)
    r1 = json.dumps(run_experiment(*args), sort_keys=True, default=str)
    r2 = json.dumps(run_experiment(*args), sort_keys=True, default=str)
    assert r1 == r2
    rep = json.loads(r1)["full"]
    assert rep["metrics"]["days"] > 100
    assert set(rep["regimes"]) <= {"risk_on", "risk_off", "mixed", "unknown"}


def test_holdout_evaluated_once_and_last():
    store, days = random_store(["AAA", "BBB", "CCC", "SPY"])
    grid = {"top_n": [1, 2]}
    rep = run_experiment(store, ["AAA", "BBB", "CCC"], days[100], days[-1],
                         BacktestConfig(bootstrap_samples=50), "holdout", grid)
    order = rep["run_order"]
    assert order.count("holdout") == 1 and order[-1] == "holdout"
    assert order.index("holdout") > max(i for i, n in enumerate(order) if n == "validation")
    assert rep["selection"]["window"] == "validation"
    assert rep["windows"]["validation"]["end"] < rep["windows"]["holdout"]["start"]


def test_walk_forward_experiment():
    store, days = random_store(["AAA", "BBB", "SPY"])
    rep = run_experiment(store, ["AAA", "BBB"], days[100], days[-1], BacktestConfig(bootstrap_samples=50),
                         "walk-forward", {"top_n": [1, 2]}, train_days=80, test_days=40)
    assert len(rep["folds"]) >= 3
    for f in rep["folds"]:
        assert f["train"]["end"] < f["test"]["start"]


# ---- CLI -----------------------------------------------------------------------------------

def test_cli_on_seeded_db(pg_db, tmp_path):
    from app.models import MarketBar

    store, days = random_store(["AAA", "BBB", "SPY"], n=330)
    for sym, bars in store._bars.items():
        for b in bars:
            pg_db.add(MarketBar(symbol=sym, timeframe="1Day", ts=b.timestamp, open=b.open, high=b.high,
                                low=b.low, close=b.close, volume=b.volume, feed="iex"))
    pg_db.commit()
    out1, out2 = tmp_path / "a.json", tmp_path / "b.json"
    argv = ["--symbols", "AAA,BBB", "--start", "2024-09-02", "--end", "2025-03-31", "--out"]
    assert cli_main(argv + [str(out1)], session=pg_db) == 0
    assert cli_main(argv + [str(out2)], session=pg_db) == 0
    assert out1.read_text() == out2.read_text()
    report = json.loads(out1.read_text())
    assert report["full"]["metrics"]["days"] > 100
    assert "benchmark_relative_return" in report["full"]["metrics"]
    assert cli_main(["--symbols", "ZZZ", "--start", "2024-09-02", "--end", "2025-03-31"],
                    session=pg_db) == 2


def test_bar_timestamp_type():
    b = make_bar(date(2025, 1, 2), 1, 2, 0.5, 1.5)
    assert isinstance(b, Bar) and b.timestamp == datetime(2025, 1, 2, tzinfo=timezone.utc)
