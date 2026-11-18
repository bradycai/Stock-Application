"""Performance metrics (pure). Returns are fractions unless a name ends in `_pct`."""
import math
import random
from datetime import date
from statistics import mean, stdev

TRADING_DAYS = 252


def daily_returns(curve: list[tuple[date, float]]) -> list[float]:
    return [curve[i][1] / curve[i - 1][1] - 1 for i in range(1, len(curve)) if curve[i - 1][1] > 0]


def total_return(curve: list[tuple[date, float]]) -> float:
    return curve[-1][1] / curve[0][1] - 1 if len(curve) > 1 else 0.0


def cagr(curve: list[tuple[date, float]]) -> float:
    if len(curve) < 2:
        return 0.0
    years = (curve[-1][0] - curve[0][0]).days / 365.25
    ratio = curve[-1][1] / curve[0][1]
    if years <= 0 or ratio <= 0:
        return 0.0 if ratio > 0 else -1.0
    return ratio ** (1 / years) - 1


def max_drawdown(curve: list[tuple[date, float]]) -> float:
    """Largest peak-to-trough fall as a positive fraction."""
    peak, worst = -math.inf, 0.0
    for _, v in curve:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst


def volatility(returns: list[float]) -> float:
    return stdev(returns) * math.sqrt(TRADING_DAYS) if len(returns) > 1 else 0.0


def sharpe(returns: list[float]) -> float:
    """Annualized mean/stdev of daily returns, risk-free rate 0."""
    if len(returns) < 2:
        return 0.0
    sd = stdev(returns)
    return mean(returns) / sd * math.sqrt(TRADING_DAYS) if sd > 0 else 0.0


def bootstrap_ci(values: list[float], samples: int = 1000, alpha: float = 0.05, seed: int = 0
                 ) -> tuple[float, float] | None:
    """Percentile bootstrap interval for the mean (seeded, so reproducible)."""
    if not values:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(mean(values[rng.randrange(n)] for _ in range(n)) for _ in range(samples))
    return means[int(samples * alpha / 2)], means[min(samples - 1, int(samples * (1 - alpha / 2)))]


def trade_stats(trade_returns_pct: list[float]) -> dict:
    wins = [r for r in trade_returns_pct if r > 0]
    losses = [r for r in trade_returns_pct if r <= 0]
    n = len(trade_returns_pct)
    win_rate = len(wins) / n if n else 0.0
    avg_win = mean(wins) if wins else 0.0
    avg_loss = mean(losses) if losses else 0.0
    return {
        "trades": n,
        "win_rate": win_rate,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "expectancy_pct": win_rate * avg_win + (1 - win_rate) * avg_loss if n else 0.0,
    }


def compute_metrics(curve, trades, benchmark_curve, traded_notional, exposure,
                    seed: int = 0, bootstrap_samples: int = 1000) -> dict:
    rets = daily_returns(curve)
    tr = [t.return_pct for t in trades]
    avg_equity = mean(v for _, v in curve) if curve else 0.0
    total, bench_total = total_return(curve), total_return(benchmark_curve)
    ci = bootstrap_ci(tr, bootstrap_samples, seed=seed)
    return {
        "net_total_return": total,
        "cagr": cagr(curve),
        "benchmark_total_return": bench_total,
        "benchmark_cagr": cagr(benchmark_curve),
        "benchmark_relative_return": total - bench_total,
        "max_drawdown": max_drawdown(curve),
        "volatility": volatility(rets),
        "sharpe": sharpe(rets),
        **trade_stats(tr),
        "mean_trade_return_pct": mean(tr) if tr else 0.0,
        "mean_trade_return_ci95_pct": list(ci) if ci else None,
        "turnover": traded_notional / 2 / avg_equity if avg_equity > 0 else 0.0,
        "exposure": mean(exposure) if exposure else 0.0,
        "days": len(curve),
    }


def regime_breakdown(curve, trades, regime_by_date: dict) -> dict:
    """Daily returns are attributed to the regime known at the PREVIOUS close; trades to the
    regime at the decision that opened them."""
    days: dict[str, list[float]] = {}
    for i in range(1, len(curve)):
        prev_d, prev_v = curve[i - 1]
        if prev_v > 0:
            days.setdefault(regime_by_date.get(prev_d, "unknown"), []).append(curve[i][1] / prev_v - 1)
    by_trade: dict[str, list[float]] = {}
    for t in trades:
        by_trade.setdefault(t.regime, []).append(t.return_pct)
    out = {}
    for regime in sorted(set(days) | set(by_trade)):
        r = days.get(regime, [])
        compounded = math.prod(1 + x for x in r) - 1 if r else 0.0
        out[regime] = {
            "days": len(r), "total_return": compounded,
            "mean_daily_return": mean(r) if r else 0.0, "sharpe": sharpe(r),
            **trade_stats(by_trade.get(regime, [])),
        }
    return out
