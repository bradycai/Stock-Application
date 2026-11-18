"""Market-stream DB heartbeat and the expected_workers flags. No sockets: fake streams."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import WorkerHeartbeat
from app.services.ops import collect
from app.workers import market_stream
from app.workers.market_stream import MarketStreamWorker

T0 = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)


@pytest.fixture()
def factory(pg_db):
    return sessionmaker(bind=pg_db.get_bind(), future=True)


def worker(factory, **kw):
    return MarketStreamWorker(factory, feed="iex", get_positions=lambda: [], now=lambda: T0, **kw)


class FakeStream:
    def __init__(self, *a, **kw):
        self.stopped = asyncio.Event()

    def subscribe_quotes(self, *a): pass
    def subscribe_bars(self, *a): pass
    def subscribe_news(self, *a): pass
    def unsubscribe_quotes(self, *a): pass
    def unsubscribe_bars(self, *a): pass
    def unsubscribe_news(self, *a): pass

    async def _run_forever(self):
        await self.stopped.wait()

    async def stop_ws(self):
        self.stopped.set()


def test_heartbeat_details_are_counts_only(factory, pg_db):
    w = worker(factory)
    w.symbols = {"AAPL", "MSFT"}
    asyncio.run(w.on_quote(SimpleNamespace(symbol="AAPL", bid_price=1, ask_price=2, bid_size=1,
                                           ask_size=1, timestamp=T0)))
    assert w.write_heartbeat() is True
    pg_db.expire_all()
    row = pg_db.get(WorkerHeartbeat, "market_stream")
    d = row.details
    assert row.status == "starting"
    assert d["subscribed_symbols"] == 2 and d["received"]["quotes"] == 1
    assert d["buffered"]["quotes"] == 1 and d["connection"] == "disconnected"
    assert d["last_message_at"] == T0.isoformat()
    assert "AAPL" not in str(d)


def test_stream_run_writes_heartbeat_periodically(factory, pg_db, monkeypatch):
    import alpaca.data.live as live
    monkeypatch.setattr(live, "StockDataStream", FakeStream)
    monkeypatch.setattr(live, "NewsDataStream", FakeStream)
    monkeypatch.setattr(settings, "stream_heartbeat_sec", 0.05)
    monkeypatch.setattr(settings, "stream_flush_sec", 0.05)
    monkeypatch.setattr(settings, "stream_refill_sec", 3600)
    monkeypatch.setattr(settings, "stream_symbol_refresh_sec", 3600)
    w = worker(factory, data_client=SimpleNamespace(get_stock_bars=lambda r: None))

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(w.run(stop))
        await asyncio.sleep(0.4)
        stop.set()
        await task

    asyncio.run(go())
    pg_db.expire_all()
    row = pg_db.get(WorkerHeartbeat, "market_stream")
    assert row is not None
    assert row.details["connection"] in ("connected", "disconnected")
    assert row.last_beat is not None


def test_heartbeat_failure_is_tolerated(factory, monkeypatch):
    w = worker(factory)

    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(market_stream.hb, "beat", boom)
    assert w.write_heartbeat() is False  # logged, not raised
    w.heartbeat()  # the periodic task body must not raise either
    # a failing session factory is tolerated too
    w2 = MarketStreamWorker(lambda: (_ for _ in ()).throw(RuntimeError("no db")), feed="iex")
    assert w2.write_heartbeat() is False


def _fake_settings(**kw):
    base = dict(watcher_enabled=False, research_worker_enabled=False, strategist_worker_enabled=False,
                ops_expect_market_stream=False)
    return SimpleNamespace(**{**base, **kw})


def test_expected_workers_flags(monkeypatch):
    # scheduler_enabled may not exist on Settings yet: use plain namespaces
    def expected(**kw):
        monkeypatch.setattr(collect, "settings", _fake_settings(**kw))
        return collect.expected_workers()

    assert expected() == []
    assert expected(ops_expect_market_stream=True) == ["market_stream"]
    assert expected(ops_expect_market_stream=True, scheduler_enabled=True) == ["market_stream", "scheduler"]
    assert expected(scheduler_enabled=True) == ["scheduler"]
    assert expected(scheduler_enabled=False) == []


def test_ops_expect_market_stream_defaults_off():
    assert settings.ops_expect_market_stream is False
