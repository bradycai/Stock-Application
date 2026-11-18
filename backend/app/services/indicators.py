"""Technical indicators computed from daily bars. Pure functions — no I/O."""
from statistics import mean


# --------------------------------------------------------------------------- #
# Indicator math (pure functions over close/high/low/volume lists)            #
# --------------------------------------------------------------------------- #
def sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return round(mean(closes[-period:]), 2)


def ema_series(values: list[float], period: int) -> list[float]:
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


def rsi(closes: list[float], period: int = 14) -> float | None:
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


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """Standard MACD (12/26/9): macd line, signal line, histogram."""
    if len(closes) < slow + signal:
        return {"macd": None, "signal": None, "histogram": None}
    ema_fast = ema_series(closes, fast)   # len = n - fast + 1
    ema_slow = ema_series(closes, slow)   # len = n - slow + 1
    offset = len(ema_fast) - len(ema_slow)  # align tails
    macd_line = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]
    signal_series = ema_series(macd_line, signal)
    if not signal_series:
        return {"macd": round(macd_line[-1], 4), "signal": None, "histogram": None}
    macd_val = macd_line[-1]
    signal_val = signal_series[-1]
    return {
        "macd": round(macd_val, 4),
        "signal": round(signal_val, 4),
        "histogram": round(macd_val - signal_val, 4),
    }


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    """Average True Range over `period` days (simple mean of true ranges). Used to size
    the trailing stop by volatility."""
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    return round(mean(trs[-period:]), 4)


def technical_indicators(bars: list) -> dict:
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

    last_ts = getattr(bars[-1], "timestamp", None)
    return {
        "history_days": history_days,
        "data_sufficient": True,
        "last_bar_ts": last_ts.isoformat() if last_ts else None,
        "last_close": round(last_close, 2),
        "sma_20": sma(closes, 20),
        "sma_50": sma(closes, 50),
        "sma_200": sma(closes, 200),
        "rsi_14": rsi(closes, 14),
        "atr_14": atr(highs, lows, closes, 14),
        "macd": macd(closes),
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
