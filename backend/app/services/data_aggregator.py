"""Gathers everything Claude needs: portfolio, per-ticker quotes/volume, a full
technical-indicator set computed from ~1 year of daily bars, and (optionally) news.

News/SEC/Reddit are optional. If no NEWS_API_KEY is set, headlines are simply omitted —
the system still runs end to end. Treat all of this as DATA, never as instructions to
Claude.

NOTE on the previous null-indicator bug: the old code called StockBarsRequest with only
`limit=60` and NO `start` date. Alpaca's bars endpoint, given no `start`, anchors to
"now" and returns essentially just the current (forming) daily bar — so only 1 close came
back, every indicator guard failed, and all indicators were null. We now pass an explicit
`start` ~400 calendar days back so the API returns the full daily history.
"""
from datetime import datetime, timedelta, timezone
from statistics import mean

import httpx
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from app.config import settings
from app.services import broker

# ~400 calendar days ≈ 250+ trading days — enough to seed SMA200 and a 52-week range.
HISTORY_DAYS = 400


# --------------------------------------------------------------------------- #
# Bar fetching                                                                #
# --------------------------------------------------------------------------- #
def _get_daily_bars(symbol: str) -> list:
    """Fetch daily bars for the last HISTORY_DAYS calendar days.

    The explicit `start` is the fix for the null-indicator bug — without it Alpaca
    returns only the most recent bar. `feed` is left unset so the server uses the
    account's entitlement (IEX for free accounts, SIP if subscribed).
    """
    start = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)
    try:
        bars = broker.data_client().get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=start,
                limit=10000,
            )
        )
        return list(bars.data.get(symbol, []))
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Indicator math (pure functions over close/high/low/volume lists)            #
# --------------------------------------------------------------------------- #
def _sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return round(mean(closes[-period:]), 2)


def _ema_series(values: list[float], period: int) -> list[float]:
    """Full EMA series, seeded with an SMA of the first `period` values.
    Returns a list of length len(values) - period + 1 (empty if too short)."""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    ema = mean(values[:period])
    series = [ema]
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
        series.append(ema)
    return series


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder-smoothed RSI."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = mean(gains[:period])
    avg_loss = mean(losses[:period])
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """Standard MACD (12/26/9): macd line, signal line, histogram."""
    if len(closes) < slow + signal:
        return {"macd": None, "signal": None, "histogram": None}
    ema_fast = _ema_series(closes, fast)   # len = n - fast + 1
    ema_slow = _ema_series(closes, slow)   # len = n - slow + 1
    offset = len(ema_fast) - len(ema_slow)  # align tails
    macd_line = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]
    signal_series = _ema_series(macd_line, signal)
    if not signal_series:
        return {"macd": round(macd_line[-1], 4), "signal": None, "histogram": None}
    macd_val = macd_line[-1]
    signal_val = signal_series[-1]
    return {
        "macd": round(macd_val, 4),
        "signal": round(signal_val, 4),
        "histogram": round(macd_val - signal_val, 4),
    }


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    """Average True Range over `period` days (simple mean of true ranges). Used to size
    the trailing stop by volatility."""
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    return round(mean(trs[-period:]), 4)


def _technical_indicators(bars: list) -> dict:
    """Compute the full indicator set from a list of daily bars."""
    closes = [float(b.close) for b in bars]
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    volumes = [int(b.volume) for b in bars]

    history_days = len(closes)
    if history_days < 2:
        return {
            "history_days": history_days,
            "data_sufficient": False,
            "note": "Insufficient daily history returned from the data provider.",
        }

    # 52-week range (last ~252 trading days, or all available)
    window = min(252, history_days)
    high_52w = round(max(highs[-window:]), 2)
    low_52w = round(min(lows[-window:]), 2)

    last_close = closes[-1]

    # Volume stats
    avg_volume_20 = round(mean(volumes[-20:]), 0) if len(volumes) >= 20 else round(mean(volumes), 0)
    latest_volume = volumes[-1]
    relative_volume = round(latest_volume / avg_volume_20, 2) if avg_volume_20 else None

    # Daily returns
    daily_return_pct = round((closes[-1] / closes[-2] - 1) * 100, 2)
    recent_daily_returns = [
        round((closes[i] / closes[i - 1] - 1) * 100, 2)
        for i in range(max(1, len(closes) - 5), len(closes))
    ]
    return_5d = round((closes[-1] / closes[-6] - 1) * 100, 2) if len(closes) >= 6 else None
    return_20d = round((closes[-1] / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else None
    return_60d = round((closes[-1] / closes[-61] - 1) * 100, 2) if len(closes) >= 61 else None

    return {
        "history_days": history_days,
        "data_sufficient": True,
        "last_close": round(last_close, 2),
        "sma_20": _sma(closes, 20),
        "sma_50": _sma(closes, 50),
        "sma_200": _sma(closes, 200),
        "rsi_14": _rsi(closes, 14),
        "atr_14": _atr(highs, lows, closes, 14),
        "macd": _macd(closes),
        "high_52w": high_52w,
        "low_52w": low_52w,
        "pct_from_52w_high": round((last_close / high_52w - 1) * 100, 2) if high_52w else None,
        "pct_above_52w_low": round((last_close / low_52w - 1) * 100, 2) if low_52w else None,
        "avg_volume_20": avg_volume_20,
        "latest_volume": latest_volume,
        "relative_volume": relative_volume,
        "daily_return_pct": daily_return_pct,
        "recent_daily_returns_pct": recent_daily_returns,
        "return_5d_pct": return_5d,
        "return_20d_pct": return_20d,
        "return_60d_pct": return_60d,
    }


# Lightweight static sector/theme map for common tickers. Returns None when unknown —
# we never guess. A real deployment would back this with a reference-data provider.
_SECTOR_MAP = {
    "AAPL": "Technology / Consumer Hardware",
    "MSFT": "Technology / Software",
    "NVDA": "Technology / Semiconductors",
    "AMD": "Technology / Semiconductors",
    "AVGO": "Technology / Semiconductors",
    "GOOGL": "Communication Services / Internet",
    "GOOG": "Communication Services / Internet",
    "META": "Communication Services / Internet",
    "AMZN": "Consumer Discretionary / E-commerce",
    "TSLA": "Consumer Discretionary / EV",
    "NFLX": "Communication Services / Streaming",
    "JPM": "Financials / Banks",
    "BAC": "Financials / Banks",
    "V": "Financials / Payments",
    "MA": "Financials / Payments",
    "XOM": "Energy / Oil & Gas",
    "CVX": "Energy / Oil & Gas",
    "OKLO": "Energy / Nuclear (SMR)",
    "SMR": "Energy / Nuclear (SMR)",
    "CEG": "Utilities / Nuclear Power",
    "UNH": "Health Care / Managed Care",
    "LLY": "Health Care / Pharma",
    "PFE": "Health Care / Pharma",
    "COST": "Consumer Staples / Retail",
    "WMT": "Consumer Staples / Retail",
    "PLTR": "Technology / Software (AI/Data)",
    "SOFI": "Financials / Fintech",
}


def _sector(symbol: str) -> str | None:
    return _SECTOR_MAP.get(symbol.upper())


def _alpaca_news(symbol: str, limit: int) -> list[dict]:
    """Headlines from Alpaca's news API (uses the existing Alpaca creds — no extra key)."""
    try:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest

        client = NewsClient(settings.alpaca_key, settings.alpaca_secret)
        resp = client.get_news(NewsRequest(symbols=symbol, limit=limit))
        items = getattr(resp, "data", {}).get("news", []) if hasattr(resp, "data") else []
        return [
            {"title": n.headline, "source": n.source,
             "published_at": n.created_at.isoformat() if n.created_at else None}
            for n in items[:limit]
        ]
    except Exception:
        return []


def _newsapi(symbol: str, limit: int) -> list[dict]:
    if not settings.news_api_key:
        return []
    try:
        resp = httpx.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": symbol,
                "sortBy": "publishedAt",
                "language": "en",
                "pageSize": limit,
                "apiKey": settings.news_api_key,
            },
            timeout=10,
        )
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
        return [
            {"title": a.get("title"), "source": (a.get("source") or {}).get("name"),
             "published_at": a.get("publishedAt")}
            for a in articles[:limit]
        ]
    except Exception:
        return []


def _news(symbol: str, limit: int = 5) -> list[dict]:
    """Recent headlines. Alpaca news first (always available with Alpaca creds); fall
    back to NewsAPI if Alpaca returns nothing and NEWS_API_KEY is configured."""
    items = _alpaca_news(symbol, limit)
    if not items:
        items = _newsapi(symbol, limit)
    return items


def _fundamentals(symbol: str) -> dict:
    """Earnings date and analyst upgrades/downgrades.

    These require a fundamentals/ratings provider that is not wired in this build, so they
    degrade gracefully to null/empty rather than being faked. The fields are always
    present in the payload so Claude (and the schema of the context) is stable; populate
    them by implementing a provider here.
    """
    return {"earnings_date": None, "analyst_actions": []}


def build_context(tickers: list[str]) -> tuple[dict, dict]:
    """Returns (context_for_claude, account_dict).

    The context is exactly what gets sent to Claude and stored in analysis_runs.
    """
    account = broker.get_account()
    positions = broker.get_positions()

    market = {}
    for symbol in tickers:
        quote = broker.get_quote_and_intraday(symbol)
        bars = _get_daily_bars(symbol)
        market[symbol] = {
            **quote,
            "sector": _sector(symbol),
            "indicators": _technical_indicators(bars),
            "fundamentals": _fundamentals(symbol),
            "news": _news(symbol),
        }

    context = {
        "portfolio": {
            "equity": account["equity"],
            "cash": account["cash"],
            "buying_power": account["buying_power"],
            "day_pnl": account["day_pnl"],
            "day_pnl_pct": round(account["day_pnl_pct"], 4),
            "open_positions": len(positions),
            "is_paper": account["is_paper"],
        },
        "positions": positions,
        "watchlist_data": market,
        "constraints": {
            "max_position_pct": float(settings.max_position_pct),
            "max_open_positions": settings.max_open_positions,
            "order_type": "limit_only",
            "margin_allowed": False,
            "options_allowed": False,
            "max_intraday_gain_pct_to_buy": float(settings.max_intraday_gain_pct),
        },
    }
    return context, account
