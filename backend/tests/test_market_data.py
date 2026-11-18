"""Batched market data, news storage, quality flags, snapshots and market context.
Alpaca is faked throughout — no network."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models import LatestQuote, MarketBar, NewsEvent
from app.services import data_aggregator, market_context, market_data, news_store, snapshots

NOW = datetime(2026, 11, 17, 18, 0, tzinfo=timezone.utc)


def _bar(close, ts, vol=1000.0):
    return SimpleNamespace(open=close * 0.99, high=close * 1.01, low=close * 0.98, close=close,
                           volume=vol, vwap=close, trade_count=10, timestamp=ts)


def _bars(n=260, start=100.0, step=0.2, end=NOW):
    return [_bar(start + i * step, end - timedelta(days=n - 1 - i)) for i in range(n)]


def _snap(price=101.0, open_=100.0, prev=99.0, ts=NOW):
    return SimpleNamespace(
        latest_trade=SimpleNamespace(price=price, timestamp=ts),
        latest_quote=SimpleNamespace(bid_price=price - 0.01, ask_price=price + 0.01, bid_size=1, ask_size=2,
                                     timestamp=ts),
        daily_bar=SimpleNamespace(open=open_, close=price, volume=5000, timestamp=ts),
        previous_daily_bar=SimpleNamespace(close=prev),
        minute_bar=None,
    )


class FakeData:
    def __init__(self, fail_symbols=()):
        self.snapshot_calls, self.bar_calls = [], []
        self.fail = set(fail_symbols)

    def get_stock_snapshot(self, req):
        syms = list(req.symbol_or_symbols)
        self.snapshot_calls.append(syms)
        if self.fail & set(syms):
            raise RuntimeError("chunk failed")
        return {s: _snap() for s in syms}

    def get_stock_bars(self, req):
        syms = list(req.symbol_or_symbols)
        self.bar_calls.append(syms)
        assert req.start is not None  # without start Alpaca returns only the current bar
        return SimpleNamespace(data={s: _bars() for s in syms})


@pytest.fixture()
def fake_alpaca(monkeypatch):
    fake = FakeData()
    monkeypatch.setattr(market_data.broker, "data_client", lambda: fake)
    monkeypatch.setattr(settings, "data_chunk_size", 2)
    monkeypatch.setattr(settings, "data_max_concurrency", 2)
    market_data.clear_caches()
    yield fake
    market_data.clear_caches()


# --------------------------------------------------------------------------- #
# Batched fetching                                                            #
# --------------------------------------------------------------------------- #
def test_snapshot_to_quote_prefers_last_trade_and_keeps_timestamps():
    q = market_data.snapshot_to_quote("AAPL", _snap(price=101.0, open_=100.0, prev=99.0))
    assert q["last_price"] == 101.0 and q["price_source"] == "last_trade"
    assert q["intraday_change_pct"] == pytest.approx(1.0)
    assert q["day_change_pct"] == pytest.approx(2.0202, 1e-3)
    assert q["quote_ts"] == NOW.isoformat() and q["today_open"] == 100.0 and q["volume"] == 5000


def test_snapshot_without_trade_falls_back_to_quote_mid():
    snap = _snap()
    snap.latest_trade = None
    q = market_data.snapshot_to_quote("AAPL", snap)
    assert q["price_source"] == "quote_mid" and q["last_price"] == pytest.approx(101.0)


def test_snapshots_are_fetched_in_chunks(fake_alpaca):
    out = market_data.fetch_snapshots(["A", "B", "C", "D", "E"])
    assert sorted(out) == ["A", "B", "C", "D", "E"]
    assert sorted(len(c) for c in fake_alpaca.snapshot_calls) == [1, 2, 2]  # 3 requests, not 5


def test_one_bad_symbol_does_not_blank_its_whole_chunk(fake_alpaca):
    fake_alpaca.fail = {"C"}
    out = market_data.fetch_snapshots(["A", "B", "C", "D"])
    assert sorted(out) == ["A", "B", "D"]  # D shared a chunk with C and was retried alone


def test_daily_bars_are_cached(fake_alpaca):
    first = market_data.fetch_daily_bars(["A", "B"])
    second = market_data.fetch_daily_bars(["A", "B"])
    assert len(fake_alpaca.bar_calls) == 1 and second["A"] is first["A"]


def test_news_is_capped_per_symbol_and_paginated(monkeypatch):
    pages = [
        SimpleNamespace(data={"news": [SimpleNamespace(id=i, headline=f"AAPL story {i}", source="x", url=None,
                                                       summary=None, created_at=NOW, updated_at=NOW,
                                                       symbols=["AAPL"], author=None, content=None)
                                       for i in range(3)]}, next_page_token="p2"),
        SimpleNamespace(data={"news": [SimpleNamespace(id=9, headline="MSFT story", source="x", url=None,
                                                       summary=None, created_at=NOW, updated_at=NOW,
                                                       symbols=["MSFT", "AAPL"], author=None, content=None)]},
                        next_page_token=None),
    ]

    class FakeNews:
        def __init__(self, *_):
            self.n = 0

        def get_news(self, req):
            assert req.include_content is True
            page = pages[self.n]
            self.n += 1
            return page
    import alpaca.data.historical.news as news_mod
    monkeypatch.setattr(news_mod, "NewsClient", FakeNews)
    out = market_data.fetch_news(["AAPL", "MSFT"], per_symbol=2)
    assert [a["external_id"] for a in out] == ["0", "1", "9"]  # AAPL capped at 2; MSFT got its story


# --------------------------------------------------------------------------- #
# Quality flags                                                               #
# --------------------------------------------------------------------------- #
def _entry(**over):
    base = {"last_price": 100.0, "quote_ts": (NOW - timedelta(seconds=30)).isoformat(),
            "indicators": {"data_sufficient": True, "sma_20": 1, "sma_50": 1, "sma_200": 1, "rsi_14": 50,
                           "macd": {"histogram": 0.1}, "atr_14": 1, "relative_volume": 1},
            "fundamentals": {"earnings_date": None, "analyst_actions": None}}
    base.update(over)
    return base


def _flags(entry, market_open=True):
    return snapshots.quality_flags(entry, market_open=market_open, now=NOW, max_quote_age_sec=120)


def test_fresh_complete_data_is_not_flagged_but_missing_fields_are_listed():
    q = _flags(_entry())
    assert q["stale"] is False and q["incomplete"] is False and q["quote_age_sec"] == 30.0
    assert q["missing"] == ["fundamentals.earnings_date", "fundamentals.analyst_actions"]


def test_old_quote_is_stale_only_while_the_market_is_open():
    old = _entry(quote_ts=(NOW - timedelta(minutes=10)).isoformat())
    assert _flags(old, market_open=True)["stale"] is True
    assert _flags(old, market_open=False)["stale"] is False
    assert _flags(_entry(quote_ts=None), market_open=True)["stale"] is True  # no timestamp at all


def test_missing_price_or_history_is_incomplete():
    assert _flags(_entry(last_price=0))["incomplete"] is True
    assert _flags(_entry(indicators={"data_sufficient": False}))["incomplete"] is True
    assert "sma_200" in _flags(_entry(indicators={"data_sufficient": True, "sma_200": None}))["missing"]


# --------------------------------------------------------------------------- #
# News classification / dedup                                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("headline,kind", [
    ("Apple Q3 Earnings Beat Estimates", "earnings"),
    ("Morgan Stanley Upgrades Nvidia To Overweight", "analyst_rating"),
    ("Pfizer To Acquire Biotech In $5B Deal", "m_and_a"),
    ("FDA Approves New Drug From Lilly", "regulatory"),
    ("Company Announces $200M Public Offering", "offering"),
    ("Tesla Shares Surge After Delivery Numbers", "market_move"),
    ("A Quiet Day", "other"),
])
def test_event_classification(headline, kind):
    assert news_store.classify_event(headline) == kind


def test_dedup_hash_ignores_case_and_punctuation_but_not_the_day():
    a = news_store.dedup_hash("Apple beats estimates!", NOW)
    assert a == news_store.dedup_hash("APPLE  beats estimates", NOW.isoformat())
    assert a != news_store.dedup_hash("Apple beats estimates", NOW + timedelta(days=1))


# --------------------------------------------------------------------------- #
# Market context                                                              #
# --------------------------------------------------------------------------- #
def test_market_context_trend_breadth_and_regime():
    up, down = _bars(step=0.2), _bars(start=200, step=-0.3)
    bars = {"SPY": up, "QQQ": up, "XLK": up, "XLF": up, "XLE": down}
    mc = market_context.compute(bars, ["SPY", "QQQ", "XLK", "XLF", "XLE", "XLV"])
    assert mc["indices"]["SPY"]["above_sma200"] is True
    assert mc["sector_breadth_pct_above_sma50"] == pytest.approx(66.7)
    assert mc["regime_hint"] == "risk_on" and mc["laggards_20d"][0] == "XLE"
    assert mc["missing"] == ["XLV"] and mc["classification_version"] == "mc-v1"


# --------------------------------------------------------------------------- #
# build_context end to end (no database)                                      #
# --------------------------------------------------------------------------- #
def test_build_context_without_db_flags_quality_and_labels_missing(fake_alpaca, monkeypatch):
    monkeypatch.setattr(market_data.broker, "get_account", lambda: {
        "equity": 1e5, "cash": 1e5, "buying_power": 1e5, "day_pnl": 0, "day_pnl_pct": 0, "is_paper": True})
    monkeypatch.setattr(market_data.broker, "get_positions", lambda: [])
    monkeypatch.setattr(market_data, "market_clock", lambda: {"is_open": False, "timestamp": None,
                                                              "next_open": None, "next_close": None})
    monkeypatch.setattr(market_data, "fetch_news", lambda syms: [
        {"source": "alpaca", "external_id": "1", "headline": "Apple Q3 earnings beat", "symbols": ["AAPL"],
         "published_at": NOW},
        {"source": "alpaca", "external_id": "2", "headline": "APPLE Q3 earnings beat!", "symbols": ["AAPL"],
         "published_at": NOW},  # same story twice -> shown once
    ])
    monkeypatch.setattr(settings, "market_context_symbols", "SPY")
    fake_alpaca.fail = {"ZZZZ"}
    ctx, _ = data_aggregator.build_context(["AAPL", "ZZZZ"])
    aapl, zzzz = ctx["watchlist_data"]["AAPL"], ctx["watchlist_data"]["ZZZZ"]
    assert aapl["quality"]["incomplete"] is False and len(aapl["news"]) == 1
    assert aapl["news"][0]["event_type"] == "earnings"
    assert aapl["fundamentals"] == {"earnings_date": None, "analyst_actions": None}
    assert zzzz["quality"]["incomplete"] is True and "last_price" in zzzz["quality"]["missing"]
    assert ctx["market"]["open"] is False and "coverage" in ctx["market"]
    assert ctx["market_context"]["indices"]["SPY"]["last"] > 0


# --------------------------------------------------------------------------- #
# Persistence (needs TEST_DATABASE_URL)                                       #
# --------------------------------------------------------------------------- #
def _article(ext, headline, symbols, source="alpaca"):
    return {"source": source, "external_id": ext, "headline": headline, "symbols": symbols,
            "published_at": NOW, "summary": "s", "content": "<p>full text</p>", "url": f"https://x/{ext}"}


def test_news_ingest_dedupes_across_sources_and_flags_new(pg_db):
    first = news_store.ingest_news(pg_db, [
        _article("1", "Nvidia Upgraded To Buy", ["NVDA"]),
        _article("n-9", "NVIDIA upgraded to buy", ["AMD"], source="newsapi"),  # same story, other source
    ], run_id="run-1")
    pg_db.commit()
    assert pg_db.query(NewsEvent).count() == 1
    row = pg_db.query(NewsEvent).one()
    assert row.symbols == ["AMD", "NVDA"] and row.content == "<p>full text</p>"
    assert row.first_seen_run_id == "run-1"
    assert first["NVDA"][0]["is_new"] is True and first["NVDA"][0]["id"] == f"news:{row.id}"

    again = news_store.ingest_news(pg_db, [_article("1", "Nvidia Upgraded To Buy", ["NVDA"])], run_id="run-2")
    assert again["NVDA"][0]["is_new"] is False and pg_db.query(NewsEvent).count() == 1


def test_bars_and_quotes_are_stored_idempotently(pg_db):
    bars = {"AAPL": _bars(n=30)}
    quotes = {"AAPL": market_data.snapshot_to_quote("AAPL", _snap())}
    assert snapshots.store_market_data(pg_db, bars, quotes, feed="iex")["bars_written"] == 30
    pg_db.commit()
    # A second run only rewrites from the latest stored bar on (the forming daily bar).
    assert snapshots.store_market_data(pg_db, bars, quotes, feed="iex")["bars_written"] == 1
    pg_db.commit()
    assert pg_db.query(MarketBar).count() == 30
    assert pg_db.get(LatestQuote, "AAPL").last_trade_price == 101.0


def test_snapshot_round_trip_is_replayable(pg_db):
    ctx = {"market": {"open": True, "feed": "iex"},
           "watchlist_data": {"AAPL": {"last_price": 1.0, "quality": {"stale": False}}}}
    snap = snapshots.save_snapshot(pg_db, purpose="analysis", run_id="run-1", as_of=NOW, context=ctx)
    pg_db.commit()
    assert snapshots.load_context(pg_db, snap.id) == ctx
    assert snap.quality == {"AAPL": {"stale": False}} and snap.content_hash == snapshots.content_hash(ctx)


def test_large_bar_writes_stay_under_the_postgres_parameter_limit(pg_db):
    bars = {f"S{i:02d}": _bars(n=300) for i in range(25)}  # 7,500 rows x 13 columns ~ 97K params
    assert snapshots.store_market_data(pg_db, bars, {}, feed="iex")["bars_written"] == 7500
    pg_db.commit()
    assert pg_db.query(MarketBar).count() == 7500


def test_a_failed_market_data_write_does_not_roll_back_stored_news(pg_db, fake_alpaca, monkeypatch):
    monkeypatch.setattr(market_data.broker, "get_account", lambda: {
        "equity": 1e5, "cash": 1e5, "buying_power": 1e5, "day_pnl": 0, "day_pnl_pct": 0, "is_paper": True})
    monkeypatch.setattr(market_data.broker, "get_positions", lambda: [])
    monkeypatch.setattr(market_data, "market_clock", lambda: {"is_open": True, "timestamp": None,
                                                              "next_open": None, "next_close": None})
    monkeypatch.setattr(market_data, "fetch_news",
                        lambda syms: [_article("1", "Apple Q3 earnings beat", ["AAPL"])])
    monkeypatch.setattr(settings, "market_context_symbols", "SPY")

    def boom(*_a, **_k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(snapshots, "store_market_data", boom)
    ctx, _ = data_aggregator.build_context(["AAPL"], db=pg_db, run_id="r1")
    pg_db.commit()
    assert pg_db.query(NewsEvent).count() == 1 and ctx["watchlist_data"]["AAPL"]["news"][0]["is_new"] is True



# --------------------------------------------------------------------------- #
# Review regressions (PR 02)                                                  #
# --------------------------------------------------------------------------- #
def test_unknown_market_state_fails_closed_on_old_quotes():
    old = _entry(quote_ts=(NOW - timedelta(minutes=10)).isoformat())
    assert _flags(old, market_open=None)["stale"] is True
    assert _flags(_entry(), market_open=None)["stale"] is False  # fresh quote is fine either way


def test_history_that_ends_days_ago_is_incomplete():
    ind = {**_entry()["indicators"], "last_bar_ts": (NOW - timedelta(days=9)).isoformat()}
    q = _flags(_entry(indicators=ind), market_open=False)
    assert q["incomplete"] is True and "recent_price_history" in q["missing"]
    ok = {**ind, "last_bar_ts": (NOW - timedelta(days=3)).isoformat()}  # a long weekend is fine
    assert _flags(_entry(indicators=ok), market_open=False)["incomplete"] is False


def test_failed_news_load_is_flagged_missing_not_shown_as_no_news():
    assert "news" in _flags(_entry(news_unavailable=True))["missing"]


def test_failed_clock_call_is_not_cached(monkeypatch):
    calls = []

    def clock():
        calls.append(1)
        raise RuntimeError("timeout")
    monkeypatch.setattr(market_data.broker, "trading_client", lambda: SimpleNamespace(get_clock=clock))
    market_data.clear_caches()
    assert market_data.market_clock()["is_open"] is None
    market_data.market_clock()
    assert len(calls) == 2


def test_batch_context_replay_is_identical():
    ctx = {"positions": [{"symbol": "AAPL"}, {"symbol": "TSLA"}], "market": {"open": True},
           "watchlist_data": {"AAPL": {"last_price": 1}, "MSFT": {"last_price": 2},
                              "TSLA": {"last_price": 3}}}
    sent = snapshots.batch_context(ctx, ["AAPL", "MSFT"])
    assert sent["positions"] == [{"symbol": "AAPL"}] and sorted(sent["watchlist_data"]) == ["AAPL", "MSFT"]
    replayed = snapshots.batch_context(json_roundtrip(ctx), ["AAPL", "MSFT"])
    assert snapshots.content_hash(replayed) == snapshots.content_hash(sent)


def json_roundtrip(obj):
    import json
    return json.loads(json.dumps(obj, default=str))


def test_dedup_day_key_is_the_same_utc_day_for_every_source():
    late_pacific = "2026-11-17T20:30:00-08:00"  # = 2026-11-18 04:30 UTC
    assert news_store.dedup_hash("Story", late_pacific) == news_store.dedup_hash(
        "Story", datetime(2026, 11, 18, 4, 30, tzinfo=timezone.utc))


def test_monitor_payload_carries_quality_and_citable_news():
    from app.services import monitor
    from app.services.triggers import Trigger
    mdata = {"indicators": {}, "quality": {"stale": True},
             "news": [{"id": "news:7", "headline": "Apple beats", "event_type": "earnings", "is_new": True}]}
    snap = {"price": 100.0, "stop_price": None, "take_price": None}
    payload = monitor._compact_payload("AAPL", snap, mdata, None, [Trigger("NEWS", "1 new headline")])
    assert payload["quality"] == {"stale": True}
    assert payload["news"][0]["id"] == "news:7" and payload["news"][0]["headline"] == "Apple beats"


def test_known_story_gains_symbols_from_a_later_ingest(pg_db):
    news_store.ingest_news(pg_db, [_article("1", "Chip stocks rally", ["NVDA"])], run_id="r1")
    pg_db.commit()
    later = news_store.ingest_news(pg_db, [_article("2", "Chip Stocks Rally", ["AMD"], source="newsapi")],
                                   run_id="r2")
    pg_db.commit()
    row = pg_db.query(NewsEvent).one()
    assert row.symbols == ["AMD", "NVDA"] and later["AMD"][0]["is_new"] is False
