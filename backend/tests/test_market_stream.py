"""Streaming worker: handlers, buffering/flush, REST refill, symbol refresh, error isolation.
No sockets: streams and the data client are fakes."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.models import LatestQuote, MarketBar, NewsEvent, Watchlist
from app.workers.market_stream import MarketStreamWorker

T0 = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)


def _aware(ts):  # alpaca request models store naive UTC
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def quote(sym="AAPL", bid=100.0, ask=100.1, ts=T0):
    return SimpleNamespace(symbol=sym, bid_price=bid, ask_price=ask, bid_size=1, ask_size=2, timestamp=ts)


def bar(sym="AAPL", ts=T0, close=101.0):
    return SimpleNamespace(symbol=sym, timestamp=ts, open=100, high=102, low=99, close=close,
                           volume=1000, vwap=100.5, trade_count=10)


def news(i=1, headline="Acme raises guidance", syms=("AAPL",)):
    return SimpleNamespace(id=i, headline=headline, summary="s", content=None, url="http://x",
                           author="a", source="benzinga", symbols=list(syms), created_at=T0, updated_at=T0)


@pytest.fixture()
def factory(pg_db):
    return sessionmaker(bind=pg_db.get_bind(), future=True)


def worker(factory, **kw):
    kw.setdefault("get_positions", lambda: [])
    return MarketStreamWorker(factory, feed="iex", **kw)


def test_quote_buffer_keeps_latest_and_flushes_in_one_write(factory, pg_db):
    w = worker(factory)
    run(w.on_quote(quote(bid=1, ts=T0)))
    run(w.on_quote(quote(bid=2, ts=T0 + timedelta(seconds=1))))
    run(w.on_quote(quote(bid=0.5, ts=T0 - timedelta(seconds=5))))  # out of order: ignored
    run(w.on_quote(quote("MSFT", bid=9)))
    assert w.counts["quotes"] == 4 and w.last_message_at is not None
    assert w.flush()["quotes"] == 2
    pg_db.expire_all()
    rows = {r.symbol: r for r in pg_db.scalars(select(LatestQuote))}
    assert rows["AAPL"].bid == 2 and rows["AAPL"].source == "stream" and rows["AAPL"].feed == "iex"
    assert w.flush() == {"quotes": 0, "bars": 0, "news": 0}  # buffer drained


def test_quote_flush_keeps_snapshot_trade_fields(factory, pg_db):
    pg_db.add(LatestQuote(symbol="AAPL", last_trade_price=99.0, feed="iex", source="snapshot"))
    pg_db.commit()
    w = worker(factory)
    run(w.on_quote(quote()))
    w.flush()
    pg_db.expire_all()
    row = pg_db.get(LatestQuote, "AAPL")
    assert row.last_trade_price == 99.0 and row.bid == 100.0 and row.source == "stream"


def test_bars_idempotent_and_updated_in_place(factory, pg_db):
    w = worker(factory)
    run(w.on_bar(bar(close=101)))
    run(w.on_bar(bar(close=102)))  # same minute twice in one batch
    w.flush()
    run(w.on_bar(bar(close=103)))  # again in a later batch
    run(w.on_bar(bar(ts=T0 + timedelta(minutes=1))))
    w.flush()
    pg_db.expire_all()
    rows = pg_db.scalars(select(MarketBar).order_by(MarketBar.ts)).all()
    assert len(rows) == 2 and rows[0].close == 103
    assert rows[0].timeframe == "1Min" and rows[0].source == "stream"


def test_news_handler_ingests_and_dedups(factory, pg_db):
    w = worker(factory)
    run(w.on_news(news(1)))
    run(w.on_news(news(1)))
    run(w.on_news(news(2, "Other story", ("MSFT",))))
    assert w.flush()["news"] == 3
    pg_db.expire_all()
    assert pg_db.scalar(select(func.count()).select_from(NewsEvent)) == 2


def test_malformed_messages_do_not_raise(factory):
    w = worker(factory)
    run(w.on_quote(SimpleNamespace()))
    run(w.on_bar(SimpleNamespace(symbol="X")))
    run(w.on_news(SimpleNamespace()))
    assert w.counts["quotes"] == w.counts["bars"] == w.counts["news"] == 0


def test_flush_failure_is_isolated_and_retried(factory, pg_db):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:  # the first quote and bar writes fail
            raise RuntimeError("db down")
        return factory()

    w = worker(flaky)
    run(w.on_quote(quote()))
    run(w.on_bar(bar()))
    assert w.flush() == {"quotes": 0, "bars": 0, "news": 0}  # must not raise
    assert w.counts["flush_errors"] == 2
    assert w.flush() == {"quotes": 1, "bars": 1, "news": 0}
    pg_db.expire_all()
    assert pg_db.scalar(select(func.count()).select_from(LatestQuote)) == 1
    assert pg_db.scalar(select(func.count()).select_from(MarketBar)) == 1


class FakeData:
    def __init__(self, bars):
        self.bars, self.requests = bars, []

    def get_stock_bars(self, req):
        self.requests.append(req)
        return SimpleNamespace(data=self.bars)


def test_refill_since_last_bar_is_idempotent_and_capped(factory, pg_db):
    now = T0 + timedelta(hours=3)
    pg_db.add(MarketBar(symbol="AAPL", timeframe="1Min", ts=now - timedelta(minutes=10), open=1, high=1,
                        low=1, close=1, volume=1, feed="iex"))
    pg_db.commit()
    fake = FakeData({"AAPL": [bar(ts=now - timedelta(minutes=m)) for m in (10, 9, 8)]})
    w = worker(factory, data_client=fake, now=lambda: now)
    w.symbols = {"AAPL"}
    assert w.refill() == 3
    assert _aware(fake.requests[0].start) == now - timedelta(minutes=9)  # last stored minute + 1
    assert w.refill() == 3  # same data again: no duplicates
    pg_db.expire_all()
    assert pg_db.scalar(select(func.count()).select_from(MarketBar)) == 3

    # no stored bars: lookback is capped at 60 minutes
    fake2 = FakeData({})
    w2 = worker(factory, data_client=fake2, now=lambda: now)
    w2.symbols = {"NEW"}
    w2.refill()
    assert _aware(fake2.requests[0].start) == now - timedelta(minutes=60)


def test_refill_survives_client_error(factory):
    class Boom:
        def get_stock_bars(self, req):
            raise RuntimeError("429")

    w = worker(factory, data_client=Boom(), now=lambda: T0)
    w.symbols = {"AAPL"}
    assert w.refill() == 0


class FakeStream:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith(("subscribe_", "unsubscribe_")):
            return lambda *a: self.calls.append((name, tuple(x for x in a if isinstance(x, str))))
        raise AttributeError(name)


def test_symbol_refresh_diff(factory, pg_db):
    pg_db.add_all([Watchlist(ticker="AAPL", active=True), Watchlist(ticker="TSLA", active=False)])
    pg_db.commit()
    positions = [{"symbol": "NVDA"}]
    w = worker(factory, get_positions=lambda: positions)
    w.stock_stream, w.news_stream = FakeStream(), FakeStream()
    assert w.refresh_symbols() == (["AAPL", "NVDA"], [])
    assert ("subscribe_quotes", ("AAPL", "NVDA")) in w.stock_stream.calls
    assert ("subscribe_news", ("AAPL", "NVDA")) in w.news_stream.calls

    w.stock_stream.calls.clear()
    positions[:] = []
    pg_db.add(Watchlist(ticker="MSFT", active=True))
    pg_db.commit()
    assert w.refresh_symbols() == (["MSFT"], ["NVDA"])
    assert ("unsubscribe_quotes", ("NVDA",)) in w.stock_stream.calls
    assert w.symbols == {"AAPL", "MSFT"}
    assert w.refresh_symbols() == ([], [])


def test_positions_failure_keeps_previous_symbols(factory, pg_db):
    state = {"fail": False}

    def positions():
        if state["fail"]:
            raise RuntimeError("broker down")
        return [{"symbol": "NVDA"}]

    w = worker(factory, get_positions=positions)
    w.refresh_symbols()
    state["fail"] = True
    assert w.refresh_symbols() == ([], [])
    assert w.symbols == {"NVDA"}


def test_subscribe_failure_does_not_raise(factory, pg_db):
    pg_db.add(Watchlist(ticker="AAPL", active=True))
    pg_db.commit()

    class Bad:
        def subscribe_quotes(self, *a):
            raise RuntimeError("ws closed")

    w = worker(factory)
    w.stock_stream = Bad()
    assert w.refresh_symbols() == ([], [])
    assert w.symbols == set()  # retried next refresh


def test_heartbeat_logs_counts(factory, caplog):
    w = worker(factory)
    w.symbols = {"AAPL"}
    run(w.on_quote(quote()))
    with caplog.at_level("INFO"):
        w.heartbeat()
    rec = next(r for r in caplog.records if r.getMessage() == "stream_heartbeat")
    assert rec.quotes == 1 and rec.subscribed == 1
