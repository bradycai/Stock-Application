"""Prescreen + batching pipeline tests. Pure logic — no DB/broker/LLM/network."""
from types import SimpleNamespace

from app.services import batch, prescreener
from app.services.recommendations import dedupe_by_ticker


# --------------------------------------------------------------------------- #
# Prescreener ranking                                                         #
# --------------------------------------------------------------------------- #
def _ind(**over):
    base = dict(
        data_sufficient=True, last_close=100.0,
        sma_20=95.0, sma_50=90.0, sma_200=80.0,
        rsi_14=58.0, macd={"histogram": 0.8},
        relative_volume=2.0, return_5d_pct=4.0, return_20d_pct=8.0,
        pct_from_52w_high=-5.0,
    )
    base.update(over)
    return base


def _ticker(indicators, intraday=0.0, news=0):
    return {"intraday_change_pct": intraday, "news": [{}] * news, "indicators": indicators}


def test_prescreener_ranks_strong_above_weak():
    market = {
        # strong: uptrend, healthy RSI, positive momentum, high rel-vol, near highs, catalyst
        "STRONG": _ticker(_ind(), news=4),
        # weak: downtrend, overbought, far below highs, no volume
        "WEAK": _ticker(_ind(
            sma_20=110, sma_50=120, sma_200=130, rsi_14=82, macd={"histogram": -1.0},
            relative_volume=0.8, return_5d_pct=-6, return_20d_pct=-15, pct_from_52w_high=-45,
        )),
        # no data -> must rank last
        "NODATA": _ticker({"data_sufficient": False}),
    }
    ranked = prescreener.rank_candidates(market, top_n=10)
    order = [r.symbol for r in ranked]
    assert order[0] == "STRONG"
    assert order.index("STRONG") < order.index("WEAK") < order.index("NODATA")


def test_prescreener_top_n_limits():
    market = {f"T{i}": _ticker(_ind(return_5d_pct=i)) for i in range(20)}
    ranked = prescreener.rank_candidates(market, top_n=5)
    assert len(ranked) == 5
    # scores are sorted descending
    scores = [r.score for r in ranked]
    assert scores == sorted(scores, reverse=True)


# --------------------------------------------------------------------------- #
# Chunking                                                                    #
# --------------------------------------------------------------------------- #
def test_chunk_splits_evenly():
    assert batch.chunk(list(range(100)), 10) == [list(range(i, i + 10)) for i in range(0, 100, 10)]


def test_chunk_handles_remainder():
    chunks = batch.chunk(list(range(23)), 10)
    assert [len(c) for c in chunks] == [10, 10, 3]


# --------------------------------------------------------------------------- #
# Batched run                                                                 #
# --------------------------------------------------------------------------- #
def intent(ticker, confidence):
    return SimpleNamespace(ticker=ticker, confidence=confidence)


def test_100_tickers_not_sent_to_claude_at_once():
    symbols = [f"T{i}" for i in range(100)]
    seen_batch_sizes = []

    def analyze_fn(b):
        seen_batch_sizes.append(len(b))
        return [intent(b[0], 0.6)]

    merged, stats = batch.run_batched(symbols, 10, analyze_fn, dedupe_by_ticker, 30)
    assert stats["batches"] == 10                 # 100 / 10
    assert max(seen_batch_sizes) <= 10            # never more than batch size per call
    assert len(seen_batch_sizes) == 10            # one call per batch, not 100


def test_malformed_batch_does_not_crash_full_run():
    symbols = [f"T{i}" for i in range(30)]

    def analyze_fn(b):
        if "T10" in b:          # the second batch is "malformed"
            raise RuntimeError("json_repair_failed")
        return [intent(b[0], 0.7)]

    merged, stats = batch.run_batched(symbols, 10, analyze_fn, dedupe_by_ticker, 30)
    assert stats["batches"] == 3
    assert stats["failed_batches"] == 1
    # the two good batches still produced results
    assert len(merged) == 2


def test_dedupe_keeps_highest_confidence_across_batches():
    symbols = ["A", "B"]

    def analyze_fn(b):
        # both batches return AAPL, with different confidence; plus a unique one each
        if b == ["A"]:
            return [intent("AAPL", 0.55), intent("NVDA", 0.7)]
        return [intent("AAPL", 0.88), intent("MSFT", 0.6)]

    merged, _stats = batch.run_batched(symbols, 1, analyze_fn, dedupe_by_ticker, 30)
    by_ticker = {i.ticker: i.confidence for i in merged}
    assert by_ticker["AAPL"] == 0.88              # highest confidence kept
    assert set(by_ticker) == {"AAPL", "NVDA", "MSFT"}


def test_max_final_caps_merged_results():
    symbols = [f"T{i}" for i in range(50)]

    def analyze_fn(b):
        return [intent(f"{b[0]}_X", 0.6)]

    merged, _stats = batch.run_batched(symbols, 1, analyze_fn, dedupe_by_ticker, max_final=5)
    assert len(merged) == 5
