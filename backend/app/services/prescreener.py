"""Quantitative pre-screener.

Scores every ticker from the market data we already gather (indicators + quote + news)
and ranks them, so only the strongest candidates are sent to Claude. Pure and
deterministic — no network, no LLM — so it's cheap and unit-testable.

Higher score = more of a tradeable opportunity (momentum/trend/volume/catalyst), with
mild penalties for over-extension (parabolic RSI / far above the 52-week high).
"""
from dataclasses import dataclass


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass
class Ranked:
    symbol: str
    score: float
    components: dict


def score_ticker(data: dict) -> tuple[float, dict]:
    """Score a single ticker's market-data dict. Returns (score, component_breakdown).

    `data` is the per-symbol entry from data_aggregator (quote fields + `indicators` +
    `news`). Missing/insufficient data scores very low so it sinks to the bottom.
    """
    ind = (data or {}).get("indicators") or {}
    if not ind.get("data_sufficient"):
        return -1e9, {"reason": "insufficient_data"}

    last = ind.get("last_close") or data.get("last_price") or 0
    c = {}

    # Relative volume — unusual activity is interesting (cap the upside).
    rv = ind.get("relative_volume") or 1.0
    c["rel_volume"] = _clamp(rv - 1.0, 0, 3) * 8

    # Momentum — reward positive 5/20-day returns, capped to avoid chasing blowoffs.
    c["return_5d"] = _clamp(ind.get("return_5d_pct") or 0, -10, 15) * 1.0
    c["return_20d"] = _clamp(ind.get("return_20d_pct") or 0, -20, 30) * 0.5

    # Trend alignment — price above each SMA.
    trend = 0
    for key, pts in (("sma_20", 6), ("sma_50", 6), ("sma_200", 8)):
        sma = ind.get(key)
        if sma and last:
            trend += pts if last >= sma else -pts
    c["trend"] = trend

    # RSI — favor the constructive 45-65 band; penalize overbought/oversold extremes.
    rsi = ind.get("rsi_14")
    if rsi is None:
        c["rsi"] = 0
    elif 45 <= rsi <= 65:
        c["rsi"] = 8
    elif 65 < rsi <= 75 or 35 <= rsi < 45:
        c["rsi"] = 2
    elif rsi > 75 or rsi < 25:
        c["rsi"] = -8
    else:
        c["rsi"] = -2

    # MACD histogram — positive = bullish momentum.
    macd = ind.get("macd") or {}
    hist = macd.get("histogram")
    c["macd"] = _clamp(hist, -2, 2) * 4 if hist is not None else 0

    # Distance from 52-week high — reward proximity to highs, penalize being extended above.
    dist = ind.get("pct_from_52w_high")  # negative when below the high, ~0 at the high
    if dist is None:
        c["near_high"] = 0
    elif dist >= 0:
        c["near_high"] = -6  # at/above the prior high — extended
    elif dist >= -15:
        c["near_high"] = 8 + dist * 0.3  # within 15% of high
    else:
        c["near_high"] = _clamp(15 + dist * 0.2, -10, 0)

    # Intraday change — small positive fine; large moves add noise/risk.
    intraday = data.get("intraday_change_pct") or 0
    c["intraday"] = -abs(intraday) * 0.2

    # News headline count — potential catalyst (capped).
    c["news"] = _clamp(len(data.get("news") or []), 0, 5) * 1.5

    return round(sum(c.values()), 4), c


def rank_candidates(market: dict, top_n: int) -> list[Ranked]:
    """Rank all tickers by score (desc) and return the top N."""
    ranked = []
    for symbol, data in (market or {}).items():
        score, comps = score_ticker(data)
        ranked.append(Ranked(symbol=symbol, score=score, components=comps))
    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked[: max(0, top_n)]
