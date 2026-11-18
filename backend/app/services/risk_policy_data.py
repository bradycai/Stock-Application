"""Loads the inputs the risk policies need (sectors, daily returns, bar-based liquidity and
volatility). All reads are best-effort: a failure yields empty inputs, and the policies then
treat the missing data conservatively (unknown sector = own bucket, unknown volume = illiquid).
Non-SQLAlchemy sessions (test fakes) yield empty inputs without any query."""
import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.logging_config import log_event, safe_error
from app.models import MarketBar
from app.services import data_aggregator, indicators, market_data

logger = logging.getLogger("trading.risk_policies")

BARS_LOOKBACK = 70  # daily bars per symbol (about 60 returns + ATR/volume window)


def _bars(db, symbols: list[str]) -> dict[str, list]:
    """symbol -> daily bars, oldest first, one per timestamp."""
    if not isinstance(db, Session) or not symbols:
        return {}
    out: dict[str, list] = {}
    try:
        with db.begin_nested():
            for sym in symbols:
                rows = db.scalars(
                    select(MarketBar).where(MarketBar.symbol == sym, MarketBar.timeframe == "1Day")
                    .order_by(MarketBar.ts.desc()).limit(BARS_LOOKBACK * 2)
                ).all()
                seen, uniq = set(), []
                for r in rows:  # several feeds may store the same day
                    if r.ts not in seen:
                        seen.add(r.ts)
                        uniq.append(r)
                out[sym] = list(reversed(uniq[:BARS_LOOKBACK]))
    except Exception as e:  # noqa: BLE001
        log_event(logger, "risk_policy.bars_failed", logging.WARNING, error=safe_error(e))
        return {}
    return out


def returns_by_symbol(db, symbols: list[str]) -> dict[str, list[float | None]]:
    """Daily close-to-close returns on a shared date axis (None where a symbol has no bar
    for that day or its previous day), newest last."""
    bars = _bars(db, symbols)
    closes = {s: {b.ts: float(b.close) for b in rows} for s, rows in bars.items()}
    dates = sorted({d for c in closes.values() for d in c})
    out: dict[str, list[float | None]] = {}
    for sym, c in closes.items():
        series: list[float | None] = []
        for prev, cur in zip(dates, dates[1:]):
            a, b = c.get(prev), c.get(cur)
            series.append(b / a - 1 if a and b else None)
        out[sym] = series
    return out


def sector_of(db, hints: dict[str, str | None] | None = None) -> Callable[[str], str | None]:
    """symbol -> sector (hints first, then the companies table, then the static map); cached."""
    cache: dict[str, str | None] = {k: v for k, v in (hints or {}).items() if v}

    def lookup(symbol: str) -> str | None:
        if symbol not in cache:
            try:
                cache[symbol] = data_aggregator._sector_for(symbol, db if isinstance(db, Session) else None)
            except Exception as e:  # noqa: BLE001
                log_event(logger, "risk_policy.sector_failed", logging.WARNING, error=safe_error(e))
                cache[symbol] = None
        return cache[symbol]

    return lookup


def market_from_indicators(mdata: dict | None, market_open: bool | None) -> dict:
    """Policy market inputs from a per-ticker context entry (analysis / monitor)."""
    mdata = mdata or {}
    ind = mdata.get("indicators") or {}
    return {"last_price": mdata.get("last_price") or ind.get("last_close"),
            "atr_14": ind.get("atr_14"), "avg_volume_20": ind.get("avg_volume_20"),
            "market_open": market_open}


def market_from_bars(db, symbol: str, price: float | None) -> dict:
    """Policy market inputs from stored daily bars (execution-time re-check)."""
    rows = _bars(db, [symbol]).get(symbol) or []
    out: dict = {"last_price": price, "market_open": None}
    if len(rows) >= 2:
        highs, lows, closes = ([float(getattr(b, k)) for b in rows] for k in ("high", "low", "close"))
        vols = [float(b.volume) for b in rows][-20:]
        out["atr_14"] = indicators.atr(highs, lows, closes, 14)
        out["avg_volume_20"] = sum(vols) / len(vols)
        out["last_price"] = price or closes[-1]
    if settings.block_when_market_closed:
        out["market_open"] = market_data.market_clock().get("is_open")
    return out
