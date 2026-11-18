"""Orchestrates backtests over windows and splits; returns JSON-serializable reports."""
import itertools
from datetime import date

from app.backtest.data import BarStore
from app.backtest.engine import BacktestConfig, buy_and_hold, run_backtest
from app.backtest.metrics import compute_metrics, regime_breakdown
from app.backtest.splits import Window, chronological_split, slice_dates, walk_forward_windows
from app.backtest.strategies import MomentumStrategy, Strategy

DEFAULT_GRID = {"top_n": [2, 3], "take_profit_pct": [8.0, 15.0], "stop_loss_pct": [4.0, 7.0]}


def momentum_factory(universe: list[str], config: BacktestConfig) -> Strategy:
    return MomentumStrategy(universe, config.top_n, config.rebalance_days, config.stop_loss_pct,
                            config.take_profit_pct, config.max_hold_days)


def run_window(store: BarStore, universe: list[str], dates: list[date], config: BacktestConfig,
               strategy_factory=momentum_factory, include_trades: bool = True) -> dict:
    if not dates:
        raise ValueError("no trading dates in window")
    res = run_backtest(store, strategy_factory(universe, config), universe, dates, config)
    bench = buy_and_hold(store, config.benchmark, dates, config)
    report = {
        "start": dates[0].isoformat(), "end": dates[-1].isoformat(),
        "metrics": compute_metrics(res.equity_curve, res.trades, bench, res.traded_notional,
                                   res.exposure, config.seed, config.bootstrap_samples),
        "regimes": regime_breakdown(res.equity_curve, res.trades, res.regime_by_date),
        "benchmark_equity_end": bench[-1][1] if bench else None,
        "risk_rejections": dict(sorted(res.rejections.items())),
        "signals": res.signals,
        "halted_days": [d.isoformat() for d in res.halted_days],
    }
    if include_trades:
        report["trades"] = [t.to_dict() for t in res.trades]
    return report


def expand_grid(grid: dict[str, list]) -> list[dict]:
    keys = sorted(grid)
    return [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]


def _select(store, universe, dates, config, grid, window: Window, factory, order: list[str]) -> tuple:
    """Pick the params with the best Sharpe on `window` (ties: first in grid order)."""
    best = None
    table = []
    for params in expand_grid(grid):
        order.append(window.name)
        rep = run_window(store, universe, slice_dates(dates, window), config.with_params(**params),
                         factory, include_trades=False)
        score = rep["metrics"]["sharpe"]
        table.append({"params": params, "sharpe": score})
        if best is None or score > best[0]:
            best = (score, params)
    return best[1], table


def run_experiment(store: BarStore, universe: list[str], start: date, end: date,
                   config: BacktestConfig, split: str = "none", grid: dict | None = None,
                   train_days: int = 120, test_days: int = 60, strategy_factory=momentum_factory
                   ) -> dict:
    grid = DEFAULT_GRID if grid is None else grid
    dates = [d for d in store.calendar(universe + [config.benchmark]) if start <= d <= end]
    out: dict = {"split": split, "config": config.to_dict(), "universe": universe,
                 "data": store.summary()}
    if split == "none":
        out["full"] = run_window(store, universe, dates, config, strategy_factory)
    elif split == "holdout":
        wins = chronological_split(dates)
        order: list[str] = []
        # Parameters are chosen on the validation window only; the holdout is not touched yet.
        params, table = _select(store, universe, dates, config, grid, wins["validation"],
                                strategy_factory, order)
        chosen = config.with_params(**params)
        out["windows"] = {k: w.to_dict() for k, w in wins.items()}
        out["selection"] = {"window": "validation", "chosen": params, "candidates": table}
        for name in ("development", "validation"):
            order.append(name)
            out[name] = run_window(store, universe, slice_dates(dates, wins[name]), chosen,
                                   strategy_factory)
        order.append("holdout")  # evaluated once, after selection
        out["holdout"] = run_window(store, universe, slice_dates(dates, wins["holdout"]), chosen,
                                    strategy_factory)
        out["run_order"] = order
    elif split == "walk-forward":
        folds = []
        order = []
        for train, test in walk_forward_windows(dates, train_days, test_days):
            params, table = _select(store, universe, dates, config, grid, train, strategy_factory, order)
            order.append(test.name)
            folds.append({
                "train": train.to_dict(), "test": test.to_dict(),
                "chosen": params, "train_candidates": table,
                "result": run_window(store, universe, slice_dates(dates, test),
                                     config.with_params(**params), strategy_factory),
            })
        if not folds:
            raise ValueError("not enough data for a walk-forward run; reduce train/test days")
        out["folds"] = folds
        out["run_order"] = order
    else:
        raise ValueError(f"unknown split '{split}'")
    return out
