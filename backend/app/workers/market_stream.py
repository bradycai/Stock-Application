"""Streaming market and news ingest worker.

Run as its own process, independent of the FastAPI app:

    python -m app.workers.market_stream

What it does
- Subscribes to quotes + minute bars (StockDataStream) and news (NewsDataStream) for the
  active watchlist plus open positions (read-only broker call), re-reading that set every
  STREAM_SYMBOL_REFRESH_SEC and subscribing/unsubscribing the difference.
- Handlers only buffer in memory. Only the latest quote per symbol is kept and flushed
  every STREAM_FLUSH_SEC in one statement; bars and news are flushed in small batches.
  A failed flush is logged and retried on the next tick; it never kills the worker.
- alpaca-py reconnects by itself. On top of that, every STREAM_REFILL_SEC a REST refill
  fetches 1Min bars missed since the last stored minute bar (at most
  STREAM_REFILL_MAX_MINUTES back). Bar writes are idempotent (uq_market_bars_key).
- A structured heartbeat is logged and written to worker_heartbeats (worker `market_stream`,
  counts only) every STREAM_HEARTBEAT_SEC. A failed heartbeat write is logged, never fatal.

This worker only reads and stores market data. It never places or changes orders.
"""
import asyncio
import logging
import signal
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.logging_config import log_event, safe_error
from app.models import LatestQuote, MarketBar, Watchlist
from app.services import market_data, news_store
from app.services.watcher import heartbeat as hb

logger = logging.getLogger(__name__)

MAX_PENDING = 5000          # cap on bars/news kept for retry after failed flushes
REFILL_CHUNK = 100
BAR_ROWS_PER_INSERT = 1000
_BAR_COLS = ("open", "high", "low", "close", "volume", "vwap", "trade_count")


def stream_feed_name() -> str:
    return (settings.alpaca_data_feed or "").lower() or "iex"


def _utc(ts):
    if isinstance(ts, datetime) and ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _f(v):
    return float(v) if v is not None else None


class MarketStreamWorker:
    def __init__(self, session_factory=None, *, feed: str | None = None, get_positions=None,
                 data_client=None, now=None):
        if session_factory is None:
            from app.database import SessionLocal
            session_factory = SessionLocal
        self.session_factory = session_factory
        self.feed = feed or stream_feed_name()
        self._get_positions = get_positions
        self._data_client = data_client
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._quotes: dict[str, dict] = {}
        self._bars: list[dict] = []
        self._news: list[dict] = []
        self.symbols: set[str] = set()
        self._positions_symbols: set[str] = set()
        self.counts = {"quotes": 0, "bars": 0, "news": 0, "flush_errors": 0}
        self.last_message_at: datetime | None = None
        self.started_at: datetime = self._now()
        self.connected = False  # set by run(): True while the stream tasks are running
        # Set by run(); used to (un)subscribe on the live streams.
        self.stock_stream = None
        self.news_stream = None

    # ------------------------------------------------------------------ handlers
    async def on_quote(self, q) -> None:
        try:
            row = {
                "symbol": q.symbol.upper(),
                "bid": _f(getattr(q, "bid_price", None)), "ask": _f(getattr(q, "ask_price", None)),
                "bid_size": _f(getattr(q, "bid_size", None)), "ask_size": _f(getattr(q, "ask_size", None)),
                "quote_ts": _utc(getattr(q, "timestamp", None)),
            }
            with self._lock:
                prev = self._quotes.get(row["symbol"])
                stale = (prev is not None and prev["quote_ts"] and row["quote_ts"]
                         and row["quote_ts"] < prev["quote_ts"])
                if not stale:
                    self._quotes[row["symbol"]] = row  # latest quote per symbol only
                self.counts["quotes"] += 1
            self.last_message_at = self._now()
        except Exception as e:  # a malformed message must not break the stream
            log_event(logger, "stream_handler_error", level=logging.WARNING, kind="quote",
                      error=safe_error(e))

    async def on_bar(self, b) -> None:
        try:
            row = market_data.bar_to_row(b.symbol.upper(), b, "1Min")
            row.update(ts=_utc(row["ts"]), feed=self.feed, adjustment="raw", source="stream")
            with self._lock:
                self._bars.append(row)
                self.counts["bars"] += 1
            self.last_message_at = self._now()
        except Exception as e:
            log_event(logger, "stream_handler_error", level=logging.WARNING, kind="bar", error=safe_error(e))

    async def on_news(self, n) -> None:
        try:
            item = market_data.news_to_dict(n)
            with self._lock:
                self._news.append(item)
                self.counts["news"] += 1
            self.last_message_at = self._now()
        except Exception as e:
            log_event(logger, "stream_handler_error", level=logging.WARNING, kind="news", error=safe_error(e))

    # --------------------------------------------------------------------- flush
    def flush(self) -> dict:
        """Write buffered data. Safe to call from a thread. Never raises."""
        with self._lock:
            quotes, self._quotes = self._quotes, {}
            bars, self._bars = self._bars, []
            news, self._news = self._news, []
        out = {"quotes": 0, "bars": 0, "news": 0}
        if quotes:
            out["quotes"] = self._guarded("quotes", lambda db: self._write_quotes(db, list(quotes.values())),
                                          lambda: self._requeue_quotes(quotes), len(quotes))
        if bars:
            out["bars"] = self._guarded("bars", lambda db: self._write_bars(db, bars),
                                        lambda: self._requeue("_bars", bars), len(bars))
        if news:
            out["news"] = self._guarded("news", lambda db: news_store.ingest_news(db, news),
                                        lambda: self._requeue("_news", news), len(news))
        return out

    def _guarded(self, kind: str, write, requeue, n: int) -> int:
        """Run `write(db)` in its own transaction; on failure roll back, requeue and log."""
        db = None
        try:
            db = self.session_factory()
            write(db)
            db.commit()
            return n
        except Exception as e:
            try:
                if db is not None:
                    db.rollback()
            except Exception:
                pass
            self.counts["flush_errors"] += 1
            requeue()
            log_event(logger, "stream_flush_error", level=logging.ERROR, kind=kind, error=safe_error(e))
            return 0
        finally:
            try:
                if db is not None:
                    db.close()
            except Exception:
                pass

    def _requeue_quotes(self, quotes: dict) -> None:
        with self._lock:
            for s, row in quotes.items():
                self._quotes.setdefault(s, row)  # keep a newer quote if one arrived meanwhile

    def _requeue(self, attr: str, rows: list) -> None:
        with self._lock:
            setattr(self, attr, (rows + getattr(self, attr))[-MAX_PENDING:])

    def _write_quotes(self, db, rows: list[dict]) -> None:
        values = [{**r, "feed": self.feed, "source": "stream"} for r in rows]
        stmt = pg_insert(LatestQuote).values(values)
        # Quote fields only: the snapshot path owns last_trade_*.
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol"],
            set_={**{c: stmt.excluded[c] for c in ("bid", "ask", "bid_size", "ask_size", "quote_ts",
                                                    "feed", "source")},
                  "updated_at": func.now()},
        )
        db.execute(stmt)

    @staticmethod
    def _write_bars(db, rows: list[dict]) -> None:
        # Collapse duplicates inside one batch (the same minute can arrive twice); last wins.
        dedup = {(r["symbol"], r["timeframe"], r["ts"], r["feed"]): r for r in rows}
        rows = list(dedup.values())
        for i in range(0, len(rows), BAR_ROWS_PER_INSERT):
            stmt = pg_insert(MarketBar).values(rows[i:i + BAR_ROWS_PER_INSERT])
            stmt = stmt.on_conflict_do_update(
                constraint="uq_market_bars_key", set_={c: stmt.excluded[c] for c in _BAR_COLS})
            db.execute(stmt)

    # ------------------------------------------------------------------- symbols
    def load_symbols(self) -> set[str]:
        db = self.session_factory()
        try:
            wl = {t.upper() for t in db.scalars(select(Watchlist.ticker).where(Watchlist.active.is_(True)))}
        finally:
            db.close()
        try:
            get_positions = self._get_positions
            if get_positions is None:
                from app.services import broker
                get_positions = broker.get_positions
            self._positions_symbols = {p["symbol"].upper() for p in get_positions()}
        except Exception as e:  # keep the last known positions rather than unsubscribing them
            log_event(logger, "stream_positions_error", level=logging.WARNING, error=safe_error(e))
        return wl | self._positions_symbols

    def refresh_symbols(self) -> tuple[list[str], list[str]]:
        """Re-read the symbol set and (un)subscribe the difference. Returns (added, removed)."""
        try:
            wanted = self.load_symbols()
        except Exception as e:
            log_event(logger, "stream_symbols_error", level=logging.ERROR, error=safe_error(e))
            return [], []
        added = sorted(wanted - self.symbols)
        removed = sorted(self.symbols - wanted)
        try:
            if added:
                self._subscribe(added)
            if removed:
                self._unsubscribe(removed)
        except Exception as e:
            log_event(logger, "stream_subscribe_error", level=logging.ERROR, error=safe_error(e))
            return [], []
        self.symbols = wanted
        if added or removed:
            log_event(logger, "stream_symbols_changed", added=added, removed=removed, total=len(wanted))
        return added, removed

    def _subscribe(self, symbols: list[str]) -> None:
        if self.stock_stream is not None:
            self.stock_stream.subscribe_quotes(self.on_quote, *symbols)
            self.stock_stream.subscribe_bars(self.on_bar, *symbols)
        if self.news_stream is not None:
            self.news_stream.subscribe_news(self.on_news, *symbols)

    def _unsubscribe(self, symbols: list[str]) -> None:
        if self.stock_stream is not None:
            self.stock_stream.unsubscribe_quotes(*symbols)
            self.stock_stream.unsubscribe_bars(*symbols)
        if self.news_stream is not None:
            self.news_stream.unsubscribe_news(*symbols)

    # -------------------------------------------------------------------- refill
    def refill(self) -> int:
        """Fetch 1Min bars missed since the last stored minute bar. Idempotent. Never raises."""
        symbols = sorted(self.symbols)
        if not symbols:
            return 0
        now = self._now()
        floor = now - timedelta(minutes=settings.stream_refill_max_minutes)
        total = 0
        try:
            db = self.session_factory()
            try:
                last = dict(db.execute(
                    select(MarketBar.symbol, func.max(MarketBar.ts))
                    .where(MarketBar.timeframe == "1Min", MarketBar.feed == self.feed,
                           MarketBar.symbol.in_(symbols))
                    .group_by(MarketBar.symbol)).all())
            finally:
                db.close()
            client = self._data_client
            if client is None:
                from app.services import broker
                client = broker.data_client()
            from alpaca.data.enums import DataFeed
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
            for i in range(0, len(symbols), REFILL_CHUNK):
                group = symbols[i:i + REFILL_CHUNK]
                start = min(max(_utc(last[s]) + timedelta(minutes=1), floor) if s in last else floor
                            for s in group)
                if start >= now:
                    continue
                try:
                    resp = client.get_stock_bars(StockBarsRequest(
                        symbol_or_symbols=group, timeframe=TimeFrame.Minute, start=start, end=now,
                        feed=DataFeed(self.feed)))
                except Exception as e:
                    log_event(logger, "stream_refill_error", level=logging.WARNING, error=safe_error(e))
                    continue
                rows = []
                for sym, bars in (getattr(resp, "data", None) or {}).items():
                    for bar in bars:
                        row = market_data.bar_to_row(sym.upper(), bar, "1Min")
                        row.update(ts=_utc(row["ts"]), feed=self.feed, adjustment="raw", source="alpaca")
                        rows.append(row)
                if rows:
                    total += self._guarded("refill", lambda d, r=rows: self._write_bars(d, r),
                                           lambda: None, len(rows))
            if total:
                log_event(logger, "stream_refill", bars=total, symbols=len(symbols))
        except Exception as e:
            log_event(logger, "stream_refill_error", level=logging.ERROR, error=safe_error(e))
        return total

    # ----------------------------------------------------------------- heartbeat
    def heartbeat(self) -> None:
        log_event(logger, "stream_heartbeat", quotes=self.counts["quotes"], bars=self.counts["bars"],
                  news=self.counts["news"], flush_errors=self.counts["flush_errors"],
                  last_message_at=self.last_message_at.isoformat() if self.last_message_at else None,
                  subscribed=len(self.symbols), symbols=sorted(self.symbols)[:50])
        self.write_heartbeat()

    def heartbeat_details(self) -> dict:
        """Counts and timestamps only: no symbols' prices, no secrets."""
        with self._lock:
            pending = {"quotes": len(self._quotes), "bars": len(self._bars), "news": len(self._news)}
        return {
            "subscribed_symbols": len(self.symbols), "feed": self.feed,
            "connection": "connected" if self.connected else "disconnected",
            "received": {k: self.counts[k] for k in ("quotes", "bars", "news")},
            "buffered": pending, "flush_errors": self.counts["flush_errors"],
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
        }

    def write_heartbeat(self) -> bool:
        """Upsert the DB heartbeat. Never raises: the stream outlives a failed write."""
        db = None
        try:
            db = self.session_factory()
            hb.beat(db, "market_stream", "running" if self.connected else "starting",
                    self.heartbeat_details(), started_at=self.started_at, now=self._now())
            db.commit()
            return True
        except Exception as e:
            try:
                if db is not None:
                    db.rollback()
            except Exception:
                pass
            log_event(logger, "stream_heartbeat_error", level=logging.WARNING, error=safe_error(e))
            return False
        finally:
            try:
                if db is not None:
                    db.close()
            except Exception:
                pass

    # ----------------------------------------------------------------------- run
    async def _periodic(self, interval: float, fn, stop: asyncio.Event, *, immediate: bool = False) -> None:
        first = immediate
        while not stop.is_set():
            if not first:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    return
                except asyncio.TimeoutError:
                    pass
            first = False
            try:
                await asyncio.to_thread(fn)
            except Exception as e:  # isolate every periodic task
                log_event(logger, "stream_task_error", level=logging.ERROR,
                          task=getattr(fn, "__name__", "?"), error=safe_error(e))

    async def _run_stream(self, stream, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await stream._run_forever()
            except Exception as e:
                log_event(logger, "stream_crashed", level=logging.ERROR, error=safe_error(e))
            if stop.is_set():
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    async def run(self, stop: asyncio.Event | None = None) -> None:
        from alpaca.data.enums import DataFeed
        from alpaca.data.live import NewsDataStream, StockDataStream

        stop = stop or asyncio.Event()
        self.stock_stream = StockDataStream(settings.alpaca_key, settings.alpaca_secret,
                                            feed=DataFeed(self.feed))
        self.news_stream = NewsDataStream(settings.alpaca_key, settings.alpaca_secret)
        # alpaca-py blocks if (un)subscribe runs on the event loop while connected, so the
        # initial subscription happens before the streams start; later changes run in a thread.
        await asyncio.to_thread(self.refresh_symbols)
        self.connected = True
        log_event(logger, "stream_worker_started", feed=self.feed, symbols=len(self.symbols))
        tasks = [
            asyncio.create_task(self._run_stream(self.stock_stream, stop)),
            asyncio.create_task(self._run_stream(self.news_stream, stop)),
            asyncio.create_task(self._periodic(settings.stream_flush_sec, self.flush, stop)),
            asyncio.create_task(self._periodic(settings.stream_symbol_refresh_sec,
                                               self.refresh_symbols, stop)),
            asyncio.create_task(self._periodic(settings.stream_refill_sec, self.refill, stop,
                                               immediate=True)),
            asyncio.create_task(self._periodic(settings.stream_heartbeat_sec, self.heartbeat, stop)),
        ]
        await stop.wait()
        log_event(logger, "stream_worker_stopping")
        self.connected = False
        for s in (self.stock_stream, self.news_stream):
            try:
                await asyncio.wait_for(s.stop_ws(), timeout=5)
            except Exception:
                pass
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(self.flush)
        log_event(logger, "stream_worker_stopped", **self.counts)


def main() -> None:
    from app.logging_config import configure_logging
    configure_logging()

    async def _main():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await MarketStreamWorker().run(stop)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
