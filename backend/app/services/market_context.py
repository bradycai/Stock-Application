"""Market context from index and sector ETFs (deterministic, from daily bars).

Gives the model the backdrop for every trade idea: index trends, how many sectors are in
uptrends (breadth), sector leaders/laggards, and realized volatility as a volatility proxy.
`regime_hint` is a simple rule-based label (CLASSIFICATION_VERSION), recorded so strategy
results can later be compared by market regime; the model-based regime analyst refines it
in a later phase.
"""
import math
from statistics import pstdev

from app.services.indicators import rsi as _rsi
from app.services.indicators import sma as _sma

CLASSIFICATION_VERSION = "mc-v1"
INDEX_ETFS = ("SPY", "QQQ", "IWM", "DIA")


def _etf_stats(bars: list) -> dict | None:
    closes = [float(b.close) for b in bars]
    if len(closes) < 21:
        return None
    last = closes[-1]
    sma50, sma200 = _sma(closes, 50), _sma(closes, 200)
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - 20, len(closes))]
    return {
        "last": round(last, 2),
        "above_sma50": last >= sma50 if sma50 else None,
        "above_sma200": last >= sma200 if sma200 else None,
        "return_5d_pct": round((last / closes[-6] - 1) * 100, 2),
        "return_20d_pct": round((last / closes[-21] - 1) * 100, 2),
        "rsi_14": _rsi(closes, 14),
        "realized_vol_20d_pct": round(pstdev(log_returns) * math.sqrt(252) * 100, 2),
        "as_of": bars[-1].timestamp.isoformat() if getattr(bars[-1], "timestamp", None) else None,
    }


def compute(bars_by_symbol: dict[str, list], symbols: list[str]) -> dict:
    stats = {s: _etf_stats(bars_by_symbol.get(s) or []) for s in symbols}
    stats = {s: v for s, v in stats.items() if v}
    indices = {s: stats[s] for s in INDEX_ETFS if s in stats}
    sectors = {s: v for s, v in stats.items() if s not in INDEX_ETFS}

    with_trend = [v for v in sectors.values() if v["above_sma50"] is not None]
    breadth = (round(100 * sum(v["above_sma50"] for v in with_trend) / len(with_trend), 1)
               if with_trend else None)
    ranked = sorted(sectors, key=lambda s: sectors[s]["return_20d_pct"], reverse=True)

    spy = indices.get("SPY")
    if not spy or breadth is None:
        hint = "unknown"
    elif spy["above_sma50"] and spy["above_sma200"] and breadth >= 60:
        hint = "risk_on"
    elif spy["above_sma200"] is False and breadth <= 40:
        hint = "risk_off"
    else:
        hint = "mixed"

    return {
        "indices": indices,
        "sectors": sectors,
        "sector_breadth_pct_above_sma50": breadth,
        "leaders_20d": ranked[:3],
        "laggards_20d": ranked[-3:][::-1] if len(ranked) >= 3 else ranked[::-1],
        "volatility_proxy_spy_20d_pct": spy["realized_vol_20d_pct"] if spy else None,
        "regime_hint": hint,
        "classification_version": CLASSIFICATION_VERSION,
        "missing": sorted(set(symbols) - set(stats)),
    }
