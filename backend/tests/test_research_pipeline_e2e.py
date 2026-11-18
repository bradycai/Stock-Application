"""End-to-end research pipeline across PR 05 and PR 06, on the real code paths:

    watcher research job → evidence bundle → five specialist analysts → bundle ready
    → Opus strategist → validated proposal → pending trade intent (never executed)

Only Claude, market data and the broker are faked. Guards the hand-off contract
(app/services/research/contracts.py) between the analysts and the strategist.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models import AnalystReportRow, IntentStatus, ResearchBundle, StrategyProposal, TradeIntent
from app.services import broker, data_aggregator, llm
from app.services.research import analysts
from app.services.research import bundle as bundle_mod
from app.services.research.contracts import AnalystReport, StrategistProposal
from app.services.strategist import pipeline
from app.services.watcher import jobs
from app.workers import research_worker

NOW = datetime.now(timezone.utc)
ISO = NOW.isoformat()
ACCOUNT = {"equity": 100000.0, "cash": 60000.0, "buying_power": 100000.0, "last_equity": 100000.0,
           "day_pnl": 0.0, "day_pnl_pct": 0.0, "is_paper": True, "is_margin": False, "raw": {}}


@pytest.fixture(autouse=True)
def no_orders(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the research pipeline must never touch order functions")
    for fn in ("submit_bracket_order", "cancel_open_orders", "place_stop_order", "close_position",
               "cancel_open_buy_orders", "close_all_positions"):
        if hasattr(broker, fn):
            monkeypatch.setattr(broker, fn, boom)


@pytest.fixture()
def market(monkeypatch):
    entry = {
        "symbol": "AAPL", "last_price": 190.0, "intraday_change_pct": 1.2, "today_open": 188.0,
        "volume": 5_000_000, "quote_ts": ISO, "last_trade_ts": ISO, "sector": "Technology",
        "indicators": {"history_days": 250, "data_sufficient": True, "last_bar_ts": ISO,
                       "last_close": 190.0, "rsi_14": 58.0, "sma_50": 180.0, "sma_200": 170.0,
                       "macd": {"macd": 1.2, "signal": 0.6}, "atr_14": 3.0, "relative_volume": 1.8},
        "fundamentals": {"earnings_date": None, "analyst_actions": None,
                         "revenue_annual": {"value": 4e11, "filed_at": "2026-10-30"}},
        "news": [{"id": "news:123", "headline": "Apple beats estimates", "summary": "Revenue up 8%",
                  "publisher": "wire", "published_at": ISO, "event_type": "earnings", "is_new": True}],
        "quality": {"stale": False, "incomplete": False, "missing": []},
    }
    ctx = {
        "portfolio": {"equity": 100000.0, "cash": 60000.0, "buying_power": 100000.0, "day_pnl": 0.0,
                      "day_pnl_pct": 0.0, "open_positions": 0, "is_paper": True},
        "positions": [],
        "market": {"open": True, "clock": {}, "feed": "iex"},
        "market_context": {"indices": {"SPY": {"last": 500.0, "as_of": ISO}}, "regime_hint": "risk_on",
                           "sector_breadth_pct_above_sma50": 70.0},
        "watchlist_data": {"AAPL": entry},
        "constraints": {"max_position_pct": 10.0},
    }
    monkeypatch.setattr(data_aggregator, "build_context", lambda tickers, *, db=None, run_id=None: (ctx, {}))
    monkeypatch.setattr(broker, "get_quote_and_intraday",
                        lambda s: {"symbol": s, "last_price": 190.0, "intraday_change_pct": 1.2})
    return ctx


@pytest.fixture()
def fake_analysts(monkeypatch):
    def call_structured(*, role, prompt_name, system, user, output_model, max_tokens=None):
        name = prompt_name.removeprefix("research_")
        ids = {"technical": ["quote:AAPL", "ind:AAPL.rsi_14"], "news": ["news:123"],
               "fundamental": ["fund:AAPL.revenue_annual"], "market_regime": ["regime:spy"]}.get(name, [])
        report = AnalystReport(symbol="AAPL", analyst=name, stance="bullish" if ids else "insufficient_data",
                               confidence=0.65, summary=f"{name} view", key_points=["k"], evidence_ids=ids,
                               risks=["r"], invalidation="close below sma_50", data_gaps=[])
        return llm.LLMResult(output=report, meta={"prompt_version": "v1", "served_model": "fake-sonnet"})
    monkeypatch.setattr(llm, "call_structured", call_structured)


def _ready_bundle(db) -> ResearchBundle:
    jobs.enqueue(db, kind="INVESTIGATE", symbol="AAPL", job_key="e2e-1", payload={"urgency": "high"})
    db.commit()
    (job,) = jobs.lease(db, "e2e", now=NOW)
    bundle = bundle_mod.build_bundle(db, job, now=NOW)
    for name in analysts.SPECS:
        analysts.run_analyst(db, bundle, name, now=NOW)
    assert research_worker.technical_succeeded(db, bundle.id)
    bundle.status = "ready"
    db.commit()
    return bundle


def _opus(proposal: StrategistProposal):
    def call(system, user):
        assert "news:123" in user and "Apple beats estimates" in user  # reports + evidence reach Opus
        meta = {"prompt_version": "strat-v1", "served_model": "fake-opus"}
        return llm.LLMResult(output=proposal, meta=meta)
    return call


def _proposal(**over) -> StrategistProposal:
    base = dict(symbol="AAPL", action="BUY", confidence=0.7, thesis="trend + earnings catalyst",
                entry_low=189.0, entry_high=190.5, invalidation_price=180.0, take_profit_price=210.0,
                position_size_percent=4.0, holding_horizon_days=10, exit_conditions=["close below 180"],
                evidence_ids=["quote:AAPL", "ind:AAPL.rsi_14", "news:123"], conflicting_evidence=[],
                risks=["guidance cut"], limitations=["IEX volume only"], abstain_reason="")
    base.update(over)
    return StrategistProposal(**base)


def test_watcher_job_becomes_a_pending_intent_through_both_workers(pg_db, market, fake_analysts):
    bundle = _ready_bundle(pg_db)
    rows = pg_db.scalars(select(AnalystReportRow).where(AnalystReportRow.bundle_id == bundle.id)).all()
    assert {r.analyst for r in rows} == set(analysts.SPECS)

    out = pipeline.process_bundle(pg_db, bundle, call_fn=_opus(_proposal()), get_account=lambda: ACCOUNT,
                                  get_positions=lambda: [], now=NOW)
    pg_db.commit()

    assert out["outcome"] == "converted", out
    prop = pg_db.scalars(select(StrategyProposal).where(StrategyProposal.bundle_id == bundle.id)).one()
    intent = pg_db.get(TradeIntent, prop.intent_id)
    assert intent.status == IntentStatus.pending_approval  # waits for approval; never executed here
    assert intent.strategy_id == "opus-strategist" and intent.market_snapshot_id == bundle.market_snapshot_id
    assert set(intent.evidence) >= {"quote:AAPL", "news:123"}
    assert 189.0 <= float(intent.limit_price) <= 190.5
    assert pg_db.get(ResearchBundle, bundle.id).status == "strategized"


def test_fabricated_evidence_from_the_strategist_never_becomes_an_intent(pg_db, market, fake_analysts):
    bundle = _ready_bundle(pg_db)
    bad = _proposal(evidence_ids=["quote:AAPL", "news:999"])  # cites an article that isn't in the bundle
    out = pipeline.process_bundle(pg_db, bundle, call_fn=_opus(bad), get_account=lambda: ACCOUNT,
                                  get_positions=lambda: [], now=NOW)
    pg_db.commit()
    prop = pg_db.scalars(select(StrategyProposal).where(StrategyProposal.bundle_id == bundle.id)).one()
    assert out["outcome"] == "rejected" and prop.status == "rejected" and prop.intent_id is None
    opus_intents = select(TradeIntent).where(TradeIntent.strategy_id == "opus-strategist")
    assert pg_db.scalars(opus_intents).first() is None


# --------------------------------------------------------------------------- #
# Regressions from the independent review of PR 06                            #
# --------------------------------------------------------------------------- #
def test_infinity_or_nan_from_the_model_fails_validation_at_the_contract():
    from pydantic import ValidationError
    raw = _proposal().model_dump()
    for bad in ("Infinity", "NaN"):
        text = __import__("json").dumps(raw)
        text = text.replace('"take_profit_price": 210.0', f'"take_profit_price": {bad}')
        with pytest.raises(ValidationError):
            StrategistProposal.model_validate_json(text)


def test_quote_dated_in_the_future_is_rejected():
    from datetime import timedelta

    from app.services.strategist import validate as v
    future = (NOW + timedelta(minutes=10)).isoformat()
    bundle = v.BundleView(symbol="AAPL", as_of=NOW, expires_at=None, holding=False, market_snapshot_id=None,
                          evidence={"quote:AAPL": {"value": {"last_price": 190.0}, "as_of": future},
                                    "news:123": {"value": "x", "as_of": ISO}})
    res = v.validate(_proposal(evidence_ids=["quote:AAPL"]), bundle, now=NOW, max_data_age_sec=600,
                     max_limit_deviation_pct=10, max_position_pct=10)
    assert res.status == "rejected" and "QUOTE_IN_FUTURE" in {x["code"] for x in res.violations}


def test_proposal_survives_a_failure_after_the_opus_call(pg_db, market, fake_analysts, monkeypatch):
    bundle = _ready_bundle(pg_db)

    def audit_boom(db, event, actor, **kw):
        if event == "strategist.proposal":
            raise RuntimeError("audit table unavailable")
        return original_audit(db, event, actor, **kw)
    original_audit = pipeline.audit
    monkeypatch.setattr(pipeline, "audit", audit_boom)
    with pytest.raises(RuntimeError):
        pipeline.process_bundle(pg_db, bundle, call_fn=_opus(_proposal()), get_account=lambda: ACCOUNT,
                                get_positions=lambda: [], now=NOW)
    pg_db.rollback()  # what the worker does on an unexpected error
    stored = pg_db.scalars(select(StrategyProposal).where(StrategyProposal.bundle_id == bundle.id)).all()
    assert len(stored) == 1  # the paid-for proposal was committed before conversion


def test_untrusted_report_text_is_clipped_in_the_prompt():
    from app.services.strategist import prompt
    clipped = prompt._clip_report({"summary": "x" * 5000, "key_points": ["y" * 5000] * 50, "confidence": 0.5})
    assert len(clipped["summary"]) <= 600 and len(clipped["key_points"]) <= prompt.LIST_MAX_ITEMS
    assert all(len(k) <= prompt.LIST_ITEM_LIMIT for k in clipped["key_points"])


def test_a_second_pending_intent_for_the_same_symbol_is_refused(pg_db, market, fake_analysts):
    first = _ready_bundle(pg_db)
    assert pipeline.process_bundle(pg_db, first, call_fn=_opus(_proposal()), get_account=lambda: ACCOUNT,
                                   get_positions=lambda: [], now=NOW)["outcome"] == "converted"
    pg_db.commit()
    jobs.enqueue(pg_db, kind="INVESTIGATE", symbol="AAPL", job_key="e2e-2", payload={})
    pg_db.commit()
    (job,) = jobs.lease(pg_db, "e2e", now=NOW)
    second = bundle_mod.build_bundle(pg_db, job, now=NOW)
    for name in analysts.SPECS:
        analysts.run_analyst(pg_db, second, name, now=NOW)
    second.status = "ready"
    pg_db.commit()
    out = pipeline.process_bundle(pg_db, second, call_fn=_opus(_proposal()), get_account=lambda: ACCOUNT,
                                  get_positions=lambda: [], now=NOW)
    pg_db.commit()
    prop = pg_db.get(StrategyProposal, out["proposal_id"])
    assert out["outcome"] == "rejected" and prop.intent_id is None
    assert "DUPLICATE_PENDING" in {v["code"] for v in prop.validation["violations"]}
