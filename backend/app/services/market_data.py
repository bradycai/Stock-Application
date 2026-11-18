"""Batched market data from Alpaca, with bounded concurrency, caching and timestamps.

Replaces the old per-symbol calls (two bar requests + one quote request per ticker, one at a
time) with multi-symbol requests:
  - snapshots (latest trade, latest quote, today's and yesterday's daily bar) — one request
    per DATA_CHUNK_SIZE symbols;
  - daily bars for ~400 days — one paginated request per chunk, cached in-process for
    BARS_CACHE_TTL_SEC (daily history barely changes intraday);
  - news — one paginated request per chunk of symbols.
Chunks run in a thread pool of at most DATA_MAX_CONCURRENCY workers. alpaca-py itself
retries 429 / 504 responses (3 attempts, 3 s apart); a chunk that still fails is logged
and its symbols come back empty, so the caller flags them as incomplete instead of the
whole run failing.

Every value keeps the timestamp the provider attached to it, so staleness can be judged
downstream (see snapshots.quality_flags). The functions that turn SDK objects into plain
dicts are pure and unit-tested.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from alpaca.data.enums import DataFeed
from alpaca.data.requests import NewsRequest, StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame

from app.config import settings
from app.logging_config import log_event, safe_error
from app.services import broker

logger = logging.getLogger("trading.data")

HISTORY_DAYS = 400  # ~275 trading days: enough to seed SMA200 and a 52-week range
NEWS_CHUNK = 20     # symbols per news request (keeps the query string short)
NEWS_MAX_PAGES = 5


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def chunks(items: list, size: int) -> list[list]:
    size = max(1, size)
    return [items[i:i + size] for i in range(0, len(items), size)]


def feed_name() -> str:
    """The feed actually requested; '' means the account default (IEX on free plans)."""
    return settings.alpaca_data_feed.lower() or "default"


def coverage_note() -> str:
    feed = feed_name()
    if feed == "sip":
        return "SIP: consolidated quotes and volume from all US exchanges"
    if feed == "iex":
        return "IEX only: about 2-3% of US volume; quotes and volume are not consolidated"
    return "account default feed (IEX on free plans: partial volume, not consolidated)"


def _feed_param():
    return DataFeed(settings.alpaca_data_feed.lower()) if settings.alpaca_data_feed else None


def _iso(ts) -> str | None:
    return ts.isoformat() if ts else None


def _merge(parts: list):
    if parts and all(isinstance(p, dict) for p in parts if p is not None):
        out = {}
        for p in parts:
            out.update(p or {})
        return out
    return [x for p in parts if p for x in p]


def _run_chunks(fn, groups: list[list[str]], what: str) -> list:
    """Run fn(group) over symbol groups with bounded concurrency. If a multi-symbol group
    fails (one bad ticker can fail a whole request), its symbols are retried one at a time
    so the good ones still come back; a symbol that fails alone yields nothing (logged)."""
    if not groups:
        return []

    def attempt(group):
        try:
            return fn(group)
        except Exception as e:  # noqa: BLE001
            log_event(logger, "data.chunk_failed", logging.WARNING, what=what, symbols=group,
                      error=safe_error(e))
            return None

    def safe(group):
        result = attempt(group)
        if result is None and len(group) > 1:
            return _merge([attempt([s]) for s in group])
        return result

    with ThreadPoolExecutor(max_workers=max(1, settings.data_max_concurrency)) as pool:
        return list(pool.map(safe, groups))


# --------------------------------------------------------------------------- #
# Market clock                                                                #
# --------------------------------------------------------------------------- #
_clock_cache: dict = {}


def market_clock(ttl_sec: float = 60) -> dict:
    """{'is_open', 'timestamp', 'next_open', 'next_close'}; None values if unavailable."""
    cached = _clock_cache.get("v")
    if cached and time.monotonic() - cached[0] < ttl_sec:
        return cached[1]
    try:
        c = broker.trading_client().get_clock()
        value = {"is_open": bool(c.is_open), "timestamp": _iso(c.timestamp),
                 "next_open": _iso(c.next_open), "next_close": _iso(c.next_close)}
    except Exception as e:  # noqa: BLE001
        log_event(logger, "data.clock_failed", logging.WARNING, error=safe_error(e))
        value = {"is_open": None, "timestamp": None, "next_open": None, "next_close": None}
    _clock_cache["v"] = (time.monotonic(), value)
    return value


# --------------------------------------------------------------------------- #
# Snapshots (latest trade / quote / daily bars)                               #
# --------------------------------------------------------------------------- #
def snapshot_to_quote(symbol: str, snap) -> dict:
    """Plain dict from an alpaca Snapshot. Keeps the keys the rest of the app already uses
    (last_price, today_open, volume, intraday_change_pct) plus provider timestamps."""
    trade = getattr(snap, "latest_trade", None)
    quote = getattr(snap, "latest_quote", None)
    daily = getattr(snap, "daily_bar", None)
    prev = getattr(snap, "previous_daily_bar", None)

    bid = float(quote.bid_price) if quote and quote.bid_price else None
    ask = float(quote.ask_price) if quote and quote.ask_price else None
    last_price, price_source = 0.0, None
    if trade and trade.price:
        last_price, price_source = float(trade.price), "last_trade"
    elif bid and ask:
        last_price, price_source = round((bid + ask) / 2, 4), "quote_mid"
    elif daily and daily.close:
        last_price, price_source = float(daily.close), "daily_close"

    today_open = float(daily.open) if daily and daily.open else None
    prev_close = float(prev.close) if prev and prev.close else None
    return {
        "symbol": symbol,
        "last_price": last_price,
        "price_source": price_source,
        "last_trade_ts": _iso(getattr(trade, "timestamp", None)),
        "bid": bid,
        "ask": ask,
        "bid_size": float(quote.bid_size) if quote and quote.bid_size is not None else None,
        "ask_size": float(quote.ask_size) if quote and quote.ask_size is not None else None,
        "quote_ts": _iso(getattr(quote, "timestamp", None)),
        "today_open": today_open,
        "volume": int(daily.volume) if daily and daily.volume is not None else None,
        "daily_bar_ts": _iso(getattr(daily, "timestamp", None)),
        "prev_close": prev_close,
        "intraday_change_pct": round((last_price - today_open) / today_open * 100, 4)
        if today_open and last_price else 0.0,
        "day_change_pct": (round((last_price / prev_close - 1) * 100, 4)
                           if prev_close and last_price else None),
    }


def fetch_snapshots(symbols: list[str]) -> dict[str, dict]:
    """Batched snapshots for many symbols. Missing symbols are simply absent."""
    def fetch(group):
        resp = broker.data_client().get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=group, feed=_feed_param()))
        if not isinstance(resp, dict):  # a single-symbol request returns the object itself
            resp = {group[0]: resp}
        return {s: snapshot_to_quote(s, snap) for s, snap in resp.items() if snap is not None}

    out: dict[str, dict] = {}
    for part in _run_chunks(fetch, chunks(list(symbols), settings.data_chunk_size), "snapshots"):
        out.update(part or {})
    return out


# --------------------------------------------------------------------------- #
# Daily bars (cached)                                                         #
# --------------------------------------------------------------------------- #
_bars_cache: dict[str, tuple[float, list]] = {}
_bars_lock = threading.Lock()


def clear_caches() -> None:
    with _bars_lock:
        _bars_cache.clear()
    _clock_cache.clear()


def fetch_daily_bars(symbols: list[str], days: int = HISTORY_DAYS) -> dict[str, list]:
    """~`days` calendar days of daily bars per symbol (alpaca Bar objects), oldest first.
    Served from an in-process cache when fresher than BARS_CACHE_TTL_SEC.

    An explicit `start` is required: without it Alpaca anchors to "now" and returns only
    the current bar, which leaves every indicator null."""
    now = time.monotonic()
    ttl = settings.bars_cache_ttl_sec
    out: dict[str, list] = {}
    missing: list[str] = []
    with _bars_lock:
        for s in symbols:
            hit = _bars_cache.get(s)
            if hit and now - hit[0] < ttl:
                out[s] = hit[1]
            else:
                missing.append(s)

    start = datetime.now(timezone.utc) - timedelta(days=days)

    def fetch(group):
        barset = broker.data_client().get_stock_bars(StockBarsRequest(
            symbol_or_symbols=group, timeframe=TimeFrame.Day, start=start, feed=_feed_param()))
        data = getattr(barset, "data", {}) or {}
        return {s: list(data.get(s, [])) for s in group}

    for part in _run_chunks(fetch, chunks(missing, settings.data_chunk_size), "daily_bars"):
        if not part:
            continue
        with _bars_lock:
            for s, bars in part.items():
                if bars:
                    _bars_cache[s] = (time.monotonic(), bars)
        out.update(part)
    return out


def bar_to_row(symbol: str, bar, timeframe: str = "1Day") -> dict:
    return {
        "symbol": symbol, "timeframe": timeframe, "ts": bar.timestamp,
        "open": float(bar.open), "high": float(bar.high), "low": float(bar.low),
        "close": float(bar.close), "volume": float(bar.volume),
        "vwap": float(bar.vwap) if getattr(bar, "vwap", None) is not None else None,
        "trade_count": float(bar.trade_count) if getattr(bar, "trade_count", None) is not None else None,
    }


# --------------------------------------------------------------------------- #
# News                                                                        #
# --------------------------------------------------------------------------- #
def news_to_dict(item) -> dict:
    return {
        "source": "alpaca",
        "external_id": str(item.id),
        "publisher": getattr(item, "source", None),
        "headline": (item.headline or "").strip(),
        "summary": (getattr(item, "summary", None) or "").strip() or None,
        "content": getattr(item, "content", None) or None,
        "url": getattr(item, "url", None),
        "author": getattr(item, "author", None),
        "symbols": [s.upper() for s in (getattr(item, "symbols", None) or [])],
        "published_at": getattr(item, "created_at", None),
        "source_updated_at": getattr(item, "updated_at", None),
    }


def fetch_news(symbols: list[str], *, lookback_hours: int | None = None,
               per_symbol: int | None = None) -> list[dict]:
    """Recent articles mentioning any of `symbols`, newest first, at most `per_symbol` per
    requested symbol (an article can count for several symbols)."""
    lookback = lookback_hours or settings.news_lookback_hours
    cap = per_symbol or settings.news_per_symbol
    start = datetime.now(timezone.utc) - timedelta(hours=lookback)

    def fetch(group):
        from alpaca.data.historical.news import NewsClient

        client = NewsClient(settings.alpaca_key, settings.alpaca_secret)
        wanted, counts, out, token = set(group), {s: 0 for s in group}, [], None
        for _ in range(NEWS_MAX_PAGES):
            resp = client.get_news(NewsRequest(symbols=",".join(group), start=start, limit=50,
                                               sort="desc", include_content=True, page_token=token))
            items = (getattr(resp, "data", {}) or {}).get("news", [])
            for item in items:
                d = news_to_dict(item)
                relevant = [s for s in d["symbols"] if s in wanted and counts[s] < cap]
                if relevant:
                    for s in relevant:
                        counts[s] += 1
                    out.append(d)
            token = getattr(resp, "next_page_token", None)
            if not token or all(c >= cap for c in counts.values()):
                break
        return out

    articles: list[dict] = []
    for part in _run_chunks(fetch, chunks(list(symbols), NEWS_CHUNK), "news"):
        articles.extend(part or [])
    return articles
