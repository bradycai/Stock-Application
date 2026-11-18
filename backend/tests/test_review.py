"""Daily review and strategy metrics. Pure stats tests need no DB; the rest use `pg_db`.
Claude is always mocked and the broker is never touched."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import (
    AnalysisRun,
    CandidateOutcome,
    IntentAction,
    IntentStatus,
    LatestQuote,
    MarketBar,
    MarketSnapshot,
    NewsEvent,
    Order,
    OrderFill,
    ReviewReport,
    StrategyMetric,
    TradeIntent,
    TradeOutcome,
)
from app.services import llm
from app.services.review import daily, metrics, summary

UTC = timezone.utc
D = date(2026, 11, 17)
T = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)


def rec(pnl, ret, hour, conf=None, sid="s1", regime="unknown", hold=2.0):
    return metrics.TradeRecord(pnl=pnl, ret_pct=ret, exit_ts=T.replace(hour=hour), strategy_id=sid,
                               strategy_version="v1", regime=regime, holding_days=hold, confidence=conf)


# --------------------------------------------------------------------------- #
# pure stats (hand computed)                                                  #
# --------------------------------------------------------------------------- #
def test_trade_stats_hand_computed():
    trades = [rec(100, 10, 14), rec(-50, -5, 15), rec(30, 3, 16)]
    s = metrics.trade_stats(trades, min_sample=3)
    assert s["sample_size"] == 3 and s["sufficient"] is True
    assert s["net_pnl"] == 80.0
    assert s["win_rate"] == pytest.approx(0.6667, abs=1e-4)
    assert s["avg_win"] == 65.0 and s["avg_loss"] == -50.0
    assert s["expectancy"] == pytest.approx(26.67, abs=0.01)
    # curve 1.1 -> 1.045 -> 1.07635 : net 7.635%, drawdown (1.1-1.045)/1.1 = 5%
    assert s["net_return_pct"] == pytest.approx(7.635, abs=1e-4)
    assert s["max_drawdown_pct"] == pytest.approx(5.0, abs=1e-4)


def test_max_drawdown_edge_cases():
    assert metrics.max_drawdown_pct([]) == 0.0
    assert metrics.max_drawdown_pct([5, 5]) == 0.0
    assert metrics.max_drawdown_pct([-10, -10]) == pytest.approx(19.0)


def test_calibration_buckets():
    trades = [rec(100, 10, 14, conf=0.75), rec(-50, -5, 15, conf=0.72), rec(30, 3, 16, conf=0.55),
              rec(5, 1, 17, conf=None)]
    cal = {c["bucket"]: c for c in metrics.calibration(trades, min_sample=2)}
    assert set(cal) == {"0.7-0.8", "0.5-0.6"}
    hi = cal["0.7-0.8"]
    assert hi["predicted_confidence"] == pytest.approx(0.735) and hi["realized_hit_rate"] == 0.5
    assert hi["gap"] == pytest.approx(0.235) and hi["sufficient"] is True
    lo = cal["0.5-0.6"]
    assert lo["realized_hit_rate"] == 1.0 and lo["sufficient"] is False


def test_bucket_labels():
    assert metrics.confidence_bucket(0.3) == "<0.5"
    assert metrics.confidence_bucket(0.95) == ">=0.9"
    assert metrics.confidence_bucket(0.6) == "0.6-0.7"
    assert metrics.confidence_bucket(None) == "unknown"
    assert metrics.holding_bucket(0.5) == "intraday" and metrics.holding_bucket(3) == "1-5d"
    assert metrics.holding_bucket(10) == "5-20d" and metrics.holding_bucket(40) == ">20d"


def test_insufficient_sample_is_flagged_not_dropped():
    rows = metrics.compute_group_metrics([rec(10, 1, 14, conf=0.8), rec(-5, -1, 15, conf=0.8)])
    assert rows and all(r["metrics"]["sufficient"] is False for r in rows)  # default min 20
    dims = {r["metrics"]["group"]["dimension"] for r in rows}
    assert {"all", "regime", "holding_period", "confidence"} <= dims
    assert "news_event_type" not in dims  # only where linked


def test_group_split_by_regime_and_strategy():
    trades = [rec(10, 1, 14, regime="risk_on"), rec(-5, -1, 15, regime="risk_off"),
              rec(7, 1, 16, sid="s2", regime="risk_on")]
    rows = metrics.compute_group_metrics(trades, min_sample=1)
    got = {(r["strategy_id"], r["metrics"]["group"]["dimension"], r["metrics"]["group"]["value"]):
           r["metrics"]["sample_size"] for r in rows}
    assert got[("s1", "all", "all")] == 2
    assert got[("s1", "regime", "risk_on")] == 1 and got[("s1", "regime", "risk_off")] == 1
    assert got[("s2", "all", "all")] == 1


# --------------------------------------------------------------------------- #
# DB fixtures                                                                 #
# --------------------------------------------------------------------------- #
def _intent(db, *, ticker="AAPL", action=IntentAction.buy, status=IntentStatus.filled, conf=0.75,
            sid="s1", created=T, snapshot_id=None, evidence=None):
    run = AnalysisRun(model="m", input_context={}, raw_response={})
    db.add(run)
    db.flush()
    i = TradeIntent(run_id=run.id, ticker=ticker, action=action, status=status, confidence=conf,
                    strategy_id=sid, strategy_version="v1", created_at=created,
                    market_snapshot_id=snapshot_id, evidence=evidence)
    db.add(i)
    db.flush()
    return i


_n = [0]


def _order(db, intent, *, status="filled", side="buy", qty=10, limit=100.0, fill=100.5, submitted=T):
    _n[0] += 1
    o = Order(intent_id=intent.id, broker_order_id=f"b{_n[0]}", client_order_id=f"c{_n[0]}",
              submitted_at=submitted, symbol=intent.ticker, side=side, qty=qty, order_type="limit",
              limit_price=limit, status=status, filled_qty=qty if fill is not None else 0,
              filled_avg_price=fill, is_live=False)
    db.add(o)
    db.flush()
    return o


def _closed(db, intent, pnl, pct, exit_ts=T, days=2.0):
    o = TradeOutcome(intent_id=intent.id, symbol=intent.ticker, qty=10, realized_pnl=pnl,
                     realized_pnl_pct=pct, exit_ts=exit_ts, holding_days=days, status="closed")
    db.add(o)
    db.flush()
    return o


def _seed_day(db):
    snap = MarketSnapshot(purpose="analysis", as_of=T, feed="iex", symbols=[], quality={},
                          content_hash="h", data={"market_context": {"regime_hint": "risk_on"}})
    db.add(snap)
    news = NewsEvent(source="x", external_id="1", headline="h", symbols=[], dedup_hash="d1",
                     event_type="earnings", classifier_version="v")
    db.add(news)
    db.flush()
    specs = [(100, 10, 0.75, 14), (-50, -5, 0.72, 15), (30, 3, 0.55, 16)]
    for n, (pnl, pct, conf, hr) in enumerate(specs):
        i = _intent(db, conf=conf, snapshot_id=snap.id if n == 0 else None,
                    evidence=[f"news:{news.id}"] if n == 0 else None)
        _closed(db, i, pnl, pct, exit_ts=T.replace(hour=hr))
    # orders: filled (30s latency), rejected, canceled, expired
    i = _intent(db)
    filled = _order(db, i, status="filled", limit=100.0, fill=100.5, qty=10)
    db.add(OrderFill(order_id=filled.id, broker_order_id=filled.broker_order_id, status="filled",
                     filled_qty=10, filled_at=T + timedelta(seconds=30)))
    for st in ("rejected", "canceled", "expired"):
        _order(db, i, status=st, fill=None)
    # open position and quote -> unrealized (103 - 100) * 10 = 30
    oi = _intent(db, ticker="MSFT")
    db.add(TradeOutcome(intent_id=oi.id, symbol="MSFT", qty=10, entry_price=100, status="open"))
    db.add(LatestQuote(symbol="MSFT", last_trade_price=103.0, feed="iex", source="snapshot"))
    # benchmark: prev close 400, day close 404 -> +1%
    for ts, c in ((T - timedelta(days=1, hours=11), 400.0), (T - timedelta(hours=11), 404.0)):
        db.add(MarketBar(symbol="SPY", timeframe="1Day", ts=ts, open=c, high=c, low=c, close=c,
                         volume=1, feed="iex"))
    # missed idea with hypothetical outcome
    w = _intent(db, ticker="NVDA", action=IntentAction.watch, status=IntentStatus.recommended)
    db.add(CandidateOutcome(intent_id=w.id, horizon_days=5, rule_version="r", start_price=100,
                            end_price=108, return_pct=8.0, benchmark_return_pct=1.0))
    db.commit()


# --------------------------------------------------------------------------- #
# report                                                                      #
# --------------------------------------------------------------------------- #
def test_daily_report_values(pg_db):
    _seed_day(pg_db)
    [rep] = daily.run_review(pg_db, D)
    d = rep.data
    assert rep.kind == "daily" and d["approval_status"] == "pending" and d["suggested_changes"] == []
    p = d["pnl"]
    assert p["realized_pnl"] == 80.0 and p["unrealized_pnl"] == 30.0 and p["fees"] == 0.0
    assert p["closed_trades"] == 3 and p["avg_win"] == 65.0 and p["avg_loss"] == -50.0
    assert p["max_drawdown_pct"] == pytest.approx(5.0)
    # slippage: filled order 100.5 vs limit 100 -> 50 bps, $5; others unfilled
    assert p["slippage_bps_avg"] == pytest.approx(50.0) and p["slippage_dollars"] == pytest.approx(5.0)
    assert p["turnover_notional"] == pytest.approx(1005.0)
    assert d["benchmark"]["benchmark_return_pct"] == pytest.approx(1.0)
    b = d["broker"]
    assert b["orders_submitted"] == 4 and b["filled"] == 1 and b["rejected"] == 1
    assert b["canceled"] == 1 and b["expired"] == 1 and b["fill_rate"] == 0.25
    assert b["submit_to_fill_seconds_avg"] == 30.0
    [m] = d["missed_ideas"]
    assert m["ticker"] == "NVDA" and m["outcomes"][0]["excess_return_pct"] == 7.0
    assert "daily review" in rep.summary and d["errors"] == []


def test_strategy_metrics_rows_regime_news_and_idempotent(pg_db):
    _seed_day(pg_db)
    daily.run_review(pg_db, D)
    rows = pg_db.execute(select(StrategyMetric)).scalars().all()
    by = {(r.metrics["group"]["dimension"], r.metrics["group"]["value"]): r.metrics for r in rows}
    assert by[("all", "all")]["sample_size"] == 3 and by[("all", "all")]["sufficient"] is False
    assert by[("regime", "risk_on")]["sample_size"] == 1 and by[("regime", "unknown")]["sample_size"] == 2
    assert by[("news_event_type", "earnings")]["sample_size"] == 1
    cal = {c["bucket"]: c for c in by[("all", "all")]["calibration"]}
    assert cal["0.7-0.8"]["realized_hit_rate"] == 0.5
    n_rows = len(rows)
    daily.run_review(pg_db, D)
    daily.run_review(pg_db, D)
    assert len(pg_db.execute(select(StrategyMetric)).scalars().all()) == n_rows
    assert len(pg_db.execute(select(ReviewReport)).scalars().all()) == 1


def test_rerun_keeps_recorded_decision(pg_db):
    _seed_day(pg_db)
    [rep] = daily.run_review(pg_db, D)
    data = dict(rep.data)
    data["approval_status"] = "approved"
    rep.data = data
    pg_db.commit()
    [again] = daily.run_review(pg_db, D)
    assert again.data["approval_status"] == "approved"


def test_empty_day_and_missing_benchmark(pg_db):
    [rep] = daily.run_review(pg_db, D)
    assert rep.data["pnl"]["closed_trades"] == 0 and rep.data["broker"]["fill_rate"] is None
    assert any("SPY" in e for e in rep.data["errors"])


def test_weekly_rollup(pg_db):
    _seed_day(pg_db)
    reports = daily.run_review(pg_db, D, weekly=True)
    assert [r.kind for r in reports] == ["daily", "weekly"]
    assert reports[1].data["pnl"]["realized_pnl"] == 80.0
    assert reports[1].period_end - reports[1].period_start == timedelta(days=7)


# --------------------------------------------------------------------------- #
# optional LLM summary                                                        #
# --------------------------------------------------------------------------- #
def test_llm_summary_off_by_default(pg_db, monkeypatch):
    assert settings.review_llm_summary is False
    called = []
    monkeypatch.setattr(llm, "call_structured", lambda **kw: called.append(kw))
    daily.run_review(pg_db, D)
    assert called == []


def test_llm_summary_stored_as_pending_proposals(pg_db, monkeypatch):
    _seed_day(pg_db)
    monkeypatch.setattr(settings, "review_llm_summary", True)
    seen = {}

    def fake(**kw):
        seen.update(kw)
        out = summary.ReviewSummary(summary="Mixed day.", suggested_changes=[
            summary.SuggestedChange(title="Tighten stops", rationale="r", strategy_id="s1", group="risk_on"),
            summary.SuggestedChange(title="General", rationale="r2"),
        ])
        return SimpleNamespace(output=out, meta={"run_id": "run-1"})

    monkeypatch.setattr(llm, "call_structured", fake)
    [rep] = daily.run_review(pg_db, D)
    assert seen["role"] == "research"
    assert "client_order_id" not in seen["user"] and "broker_order_id" not in seen["user"]
    assert "Mixed day." in rep.summary and rep.llm_run_id == "run-1"
    # the s1/risk_on group is insufficient (1 trade < 20), so that proposal is dropped
    [c] = rep.data["suggested_changes"]
    assert c["title"] == "General" and c["approval_status"] == "pending"
    assert rep.data["approval_status"] == "pending"


def test_llm_failure_is_recorded_not_raised(pg_db, monkeypatch):
    monkeypatch.setattr(settings, "review_llm_summary", True)

    def boom(**kw):
        raise llm.LLMError("api_error", "down", {})

    monkeypatch.setattr(llm, "call_structured", boom)
    [rep] = daily.run_review(pg_db, D)
    assert any("llm summary failed" in e for e in rep.data["errors"])
    assert rep.data["suggested_changes"] == []


# --------------------------------------------------------------------------- #
# API                                                                         #
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(pg_db):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.deps import require_token
    from app.main import app

    app.dependency_overrides[get_db] = lambda: pg_db
    app.dependency_overrides[require_token] = lambda: "user:test"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_reviews_api(client, pg_db):
    _seed_day(pg_db)
    daily.run_review(pg_db, D, weekly=True)
    lst = client.get("/reviews").json()
    assert {r["kind"] for r in lst} == {"daily", "weekly"}
    assert [r["kind"] for r in client.get("/reviews", params={"period": "weekly"}).json()] == ["weekly"]
    rid = next(r["id"] for r in lst if r["kind"] == "daily")
    full = client.get(f"/reviews/{rid}").json()
    assert full["data"]["pnl"]["realized_pnl"] == 80.0
    assert client.get("/reviews/99999").status_code == 404

    sm = client.get("/strategy-metrics", params={"strategy_id": "s1"}).json()
    assert sm and all(m["strategy_id"] == "s1" for m in sm)
    assert client.get("/strategy-metrics", params={"min_sample": 50}).json() == []
    assert client.get("/strategy-metrics", params={"strategy_id": "nope"}).json() == []

    r = client.post(f"/reviews/{rid}/decision", json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["approval_status"] == "approved"
    assert client.get(f"/reviews/{rid}").json()["approval_status"] == "approved"
    assert client.post(f"/reviews/{rid}/decision", json={"decision": "maybe"}).status_code == 422
    assert client.post("/reviews/99999/decision", json={"decision": "reject"}).status_code == 404


def test_reviews_require_auth():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    assert c.get("/reviews").status_code == 401
    assert c.post("/reviews/1/decision", json={"decision": "approve"}).status_code == 401
