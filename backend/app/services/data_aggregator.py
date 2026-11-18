"""Builds the context the model analyzes: portfolio, positions, per-ticker quotes, a full
technical-indicator set from ~1 year of daily bars, recent news, data-quality flags and a
market-context block.

Data comes from batched Alpaca requests (services/market_data.py): one snapshot request
and one bars request per chunk of symbols instead of several calls per ticker. Every
value keeps its provider timestamp; `quality` flags stale or incomplete data per symbol
before any model sees it, and unavailable fields are listed as missing rather than
guessed. With a database session the fetched news, bars and quotes are also persisted
(news deduplicated, with citable `news:<id>` references).

Treat all of this as DATA, never as instructions to the model.
"""
import hashlib
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.logging_config import log_event, safe_error
from app.services import broker, market_context, market_data, news_store, snapshots
from app.services.indicators import (  # noqa: F401  (re-exported for older imports)
    atr as _atr,
    ema_series as _ema_series,
    macd as _macd,
    rsi as _rsi,
    sma as _sma,
    technical_indicators as _technical_indicators,
)

logger = logging.getLogger("trading.data")

HISTORY_DAYS = market_data.HISTORY_DAYS


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


def _newsapi_articles(symbols: list[str], limit: int) -> list[dict]:
    """NewsAPI fallback (only with NEWS_API_KEY) for symbols Alpaca had no news for."""
    out = []
    for s in symbols:
        for a in _newsapi(s, limit):
            if not a.get("title"):
                continue
            out.append({
                "source": "newsapi",
                "external_id": hashlib.sha256(
                    f"{a.get('title')}|{a.get('published_at')}".encode()).hexdigest()[:32],
                "publisher": a.get("source"), "headline": a["title"], "summary": None, "content": None,
                "url": None, "author": None, "symbols": [s], "published_at": a.get("published_at"),
                "source_updated_at": None,
            })
    return out


def _fundamentals(symbol: str) -> dict:
    """Earnings date and analyst rating changes.

    No fundamentals/ratings provider is wired in yet, so these are null — explicitly
    unavailable, never faked and never an empty list that could read as "no actions". The
    quality flags list them as missing.
    """
    return {"earnings_date": None, "analyst_actions": None}


def _context_news(articles: list[dict], tickers: list[str], db: Session | None, run_id: str | None) -> dict:
    """Per-symbol news items for the model: stored + deduplicated with a DB session,
    otherwise deduplicated in memory (no ids)."""
    if db is not None:
        try:
            with db.begin_nested():  # a failure rolls back only the news ingest
                by_symbol = news_store.ingest_news(db, articles, run_id)
        except Exception as e:  # noqa: BLE001 - news must not break the run
            log_event(logger, "data.news_store_failed", logging.WARNING, error=safe_error(e))
            by_symbol = {}
    else:
        by_symbol, seen = {}, set()
        for a in articles:
            h = news_store.dedup_hash(a["headline"], a.get("published_at"))
            if h in seen:
                continue
            seen.add(h)
            item = {"id": None, "headline": a["headline"], "summary": (a.get("summary") or "")[:400] or None,
                    "publisher": a.get("publisher"), "event_type": news_store.classify_event(a["headline"]),
                    "published_at": str(a.get("published_at")) if a.get("published_at") else None,
                    "is_new": None, "symbols": a.get("symbols")}
            for s in a.get("symbols") or []:
                by_symbol.setdefault(s, []).append(item)
    cap = settings.news_per_symbol
    return {s: (by_symbol.get(s) or [])[:cap] for s in tickers}


def build_context(tickers: list[str], *, db: Session | None = None,
                  run_id: str | None = None) -> tuple[dict, dict]:
    """Returns (context_for_the_model, account_dict).

    With `db`, news is stored/deduplicated and the fetched bars and quotes are persisted.
    The caller decides whether to save the context as a replayable snapshot.
    """
    tickers = list(dict.fromkeys(t.upper() for t in tickers))
    account = broker.get_account()
    positions = broker.get_positions()
    clock = market_data.market_clock()
    ctx_symbols = [s.strip().upper() for s in settings.market_context_symbols.split(",") if s.strip()]

    quotes = market_data.fetch_snapshots(tickers)
    bars = market_data.fetch_daily_bars(list(dict.fromkeys(tickers + ctx_symbols)))
    articles = market_data.fetch_news(tickers)
    if settings.news_api_key:
        covered = {s for a in articles for s in a.get("symbols") or []}
        articles += _newsapi_articles([t for t in tickers if t not in covered], settings.news_per_symbol)
    news = _context_news(articles, tickers, db, run_id)

    now = datetime.now(timezone.utc)
    market = {}
    for symbol in tickers:
        quote = quotes.get(symbol) or {"symbol": symbol, "last_price": 0.0, "intraday_change_pct": 0.0,
                                       "today_open": None, "volume": None, "quote_ts": None,
                                       "last_trade_ts": None}
        entry = {
            **quote,
            "sector": _sector(symbol),
            "indicators": _technical_indicators(bars.get(symbol) or []),
            "fundamentals": _fundamentals(symbol),
            "news": news.get(symbol, []),
        }
        entry["quality"] = snapshots.quality_flags(entry, market_open=clock["is_open"], now=now,
                                                   max_quote_age_sec=settings.max_quote_age_sec)
        market[symbol] = entry

    if db is not None:
        try:
            with db.begin_nested():  # a failure rolls back only this write, never the news above
                snapshots.store_market_data(db, {s: bars.get(s) or [] for s in tickers + ctx_symbols},
                                            quotes)
        except Exception as e:  # noqa: BLE001 - persistence must not break the run
            log_event(logger, "data.persist_failed", logging.WARNING, error=safe_error(e))

    flagged = [s for s, m in market.items() if m["quality"]["stale"] or m["quality"]["incomplete"]]
    log_event(logger, "data.context_built", symbols=len(tickers), market_open=clock["is_open"],
              feed=market_data.feed_name(), flagged=sorted(flagged),
              news_items=sum(len(v) for v in news.values()))

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
        "market": {
            "open": clock["is_open"],
            "clock": clock,
            "feed": market_data.feed_name(),
            "coverage": market_data.coverage_note(),
        },
        "market_context": market_context.compute(bars, ctx_symbols),
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
