"""Morning briefing: digest from empty/partial data, and the optional Claude summary failing
closed (LLMError, budget block, bad calls). `llm.call_structured` is always faked."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models import (
    Fundamental, LatestQuote, LLMCall, MacroObservation, NewsEvent, PositionManagement, ReviewReport,
    SystemAlert, Watchlist,
)
from app.services import briefing, llm

# Tuesday 2026-01-06 08:30 ET
NOW = datetime(2026, 1, 6, 13, 30, tzinfo=timezone.utc)


def test_last_close_skips_weekends_and_uses_1600_et():
    assert briefing.last_close(NOW) == datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc)   # Mon 16:00 EST
    mon = datetime(2026, 1, 5, 13, 30, tzinfo=timezone.utc)
    assert briefing.last_close(mon) == datetime(2026, 1, 2, 21, 0, tzinfo=timezone.utc)   # Fri
    after = datetime(2026, 1, 6, 22, 0, tzinfo=timezone.utc)                              # Tue 17:00 ET
    assert briefing.last_close(after) == datetime(2026, 1, 6, 21, 0, tzinfo=timezone.utc)
    # DST: Mon 2026-03-09 morning looks back to Fri 16:00 EST
    assert briefing.last_close(datetime(2026, 3, 9, 12, 30, tzinfo=timezone.utc)) == \
        datetime(2026, 3, 6, 21, 0, tzinfo=timezone.utc)


def test_empty_database_gives_a_complete_empty_digest(pg_db):
    d = briefing.build_digest(pg_db, NOW)
    assert d["watchlist"] == [] and d["held"] == [] and d["quotes"] == [] and d["earnings"] == []
    assert d["macro"] == [] and d["alerts"] == [] and d["review"] is None
    assert d["news"]["total"] == 0 and d["news"]["headlines"] == []
    assert d["summary"] is None and d["llm"] == {"status": "disabled"}


def seed_partial(db):
    db.add(Watchlist(ticker="AAPL", active=True))
    db.add(PositionManagement(symbol="MSFT", is_open=True, entry_price=1, original_qty=1, highest_price=1))
    db.add(LatestQuote(symbol="AAPL", bid=99.0, ask=101.0, last_trade_price=100.0, quote_ts=NOW,
                       feed="iex", source="snapshot"))
    for i, (syms, when) in enumerate([(["AAPL"], NOW - timedelta(hours=2)),
                                      (["TSLA"], NOW - timedelta(hours=3)),            # not watched
                                      (["AAPL"], NOW - timedelta(days=3))]):           # before last close
        db.add(NewsEvent(source="alpaca", external_id=str(i), headline="H" * 500 + f" {i}", symbols=syms,
                         published_at=when, dedup_hash=f"h{i}", event_type="earnings",
                         classifier_version="t"))
    db.add(Fundamental(symbol="MSFT", metric="revenue", period_end=date(2025, 12, 31), form="10-Q",
                       value=1.0, filed_at=date(2026, 1, 7)))
    db.add(Fundamental(symbol="AAPL", metric="revenue", period_end=date(2025, 9, 30), form="10-K",
                       value=1.0, filed_at=date(2025, 11, 1)))
    db.add(MacroObservation(series_id="DGS10", observation_date=date(2026, 1, 2), value=4.1,
                            realtime_start=date(2026, 1, 3)))
    db.add(MacroObservation(series_id="OLD", observation_date=date(2024, 1, 2), value=1.0,
                            realtime_start=date(2024, 1, 3)))
    db.add(SystemAlert(kind="stale", key="x", severity="warning", message="data stale", details={},
                       status="open"))
    db.add(SystemAlert(kind="done", key="y", severity="info", message="fixed", details={},
                       status="resolved"))
    db.add(ReviewReport(kind="daily", period_start=NOW - timedelta(days=2),
                        period_end=NOW - timedelta(days=1),
                        summary="Daily review: +1.2%\nmore detail", data={}))
    db.commit()


def test_partial_data_digest(pg_db):
    seed_partial(pg_db)
    d = briefing.build_digest(pg_db, NOW)
    assert d["watchlist"] == ["AAPL"] and d["held"] == ["MSFT"]
    assert d["news"]["total"] == 2 and d["news"]["watched_or_held"] == 1
    assert d["news"]["per_symbol"] == {"AAPL": 1}
    h = d["news"]["headlines"][0]
    assert h["symbols"] == ["AAPL"] and len(h["headline"]) <= settings.briefing_clip_chars
    assert [q["symbol"] for q in d["quotes"]] == ["AAPL"] and d["quotes"][0]["last"] == 100.0
    assert [(e["symbol"], e["filed_at"]) for e in d["earnings"]] == [("MSFT", "2026-01-07")]
    assert [m["series_id"] for m in d["macro"]] == ["DGS10"]
    assert [a["kind"] for a in d["alerts"]] == ["stale"]
    assert d["review"]["headline"] == "Daily review: +1.2%"


# --------------------------------------------------------------------------- optional LLM summary
class Boom:
    def __init__(self, exc):
        self.exc, self.calls = exc, []

    def __call__(self, **kw):
        self.calls.append(kw)
        raise self.exc


def ok_call(calls):
    def fake(**kw):
        calls.append(kw)
        return llm.LLMResult(output=briefing.BriefingSummary(summary="Quiet morning.", attention=["AAPL"]),
                             meta={"served_model": "fake"})
    return fake


@pytest.fixture()
def llm_on(monkeypatch):
    monkeypatch.setattr(settings, "briefing_llm_summary", True)


def test_llm_off_by_default_makes_no_call(pg_db, monkeypatch):
    fake = Boom(AssertionError("must not be called"))
    monkeypatch.setattr(llm, "call_structured", fake)
    d = briefing.run(pg_db, NOW)
    assert fake.calls == [] and d["summary"] is None and d["llm"]["status"] == "disabled"


def test_llm_summary_success_uses_research_role_and_untrusted_framing(pg_db, llm_on, monkeypatch):
    seed_partial(pg_db)
    calls = []
    monkeypatch.setattr(llm, "call_structured", ok_call(calls))
    d = briefing.run(pg_db, NOW)
    assert d["summary"] == {"text": "Quiet morning.", "attention": ["AAPL"]}
    assert d["llm"]["status"] == "ok"
    (kw,) = calls
    assert kw["role"] == "research" and kw["output_model"] is briefing.BriefingSummary
    assert "untrusted" in kw["user"] and "<digest>" in kw["user"]
    assert "H" * 300 not in kw["user"]  # headlines are clipped


@pytest.mark.parametrize("kind", ["refusal", "incomplete", "invalid_output", "api_error"])
def test_llm_error_keeps_the_deterministic_digest(pg_db, llm_on, monkeypatch, kind):
    seed_partial(pg_db)
    fake = Boom(llm.LLMError(kind, "nope", {}))
    monkeypatch.setattr(llm, "call_structured", fake)
    d = briefing.run(pg_db, NOW)
    assert len(fake.calls) == 1  # no retry storm
    assert d["summary"] is None and d["llm"] == {"status": "error", "kind": kind}
    assert d["watchlist"] == ["AAPL"] and d["news"]["watched_or_held"] == 1  # digest intact


def test_unexpected_exception_also_fails_closed(pg_db, llm_on, monkeypatch):
    monkeypatch.setattr(llm, "call_structured", Boom(RuntimeError("weird")))
    d = briefing.run(pg_db, NOW)
    assert d["summary"] is None and d["llm"]["kind"] == "unexpected"


def test_budget_gate_blocks_the_call(pg_db, llm_on, monkeypatch):
    fake = Boom(AssertionError("must not be called"))
    monkeypatch.setattr(llm, "call_structured", fake)
    monkeypatch.setattr(settings, "research_daily_budget_usd", 0.0)
    d = briefing.run(pg_db, NOW)
    assert fake.calls == []
    assert d["summary"] is None and d["llm"] == {"status": "skipped_budget", "reason": "daily_budget"}


def test_budget_counts_existing_research_spend(pg_db, llm_on, monkeypatch):
    fake = Boom(AssertionError("must not be called"))
    monkeypatch.setattr(llm, "call_structured", fake)
    monkeypatch.setattr(settings, "research_daily_budget_usd", 5.0)
    pg_db.add(LLMCall(role="research", prompt_name="x", prompt_version="v", requested_model="m", effort="low",
                      status="ok", cost_usd=5.0,
                      created_at=NOW - timedelta(hours=1)))
    pg_db.commit()
    d = briefing.run(pg_db, NOW)
    assert fake.calls == [] and d["llm"]["status"] == "skipped_budget"


def test_briefing_job_stores_digest_even_when_llm_fails(pg_db, llm_on, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    from app.models import ScheduledJobRun
    from app.services.scheduler import runner
    monkeypatch.setattr(llm, "call_structured", Boom(llm.LLMError("api_error", "down", {})))
    factory = sessionmaker(bind=pg_db.get_bind(), future=True)
    defs = [j for j in runner.job_defs() if j.name == "briefing"]
    out = runner.run_due(factory, NOW, defs)
    assert out[0]["status"] == "succeeded"
    pg_db.expire_all()
    row = pg_db.query(ScheduledJobRun).one()
    assert row.result["summary"] is None and row.result["llm"]["status"] == "error"
    assert "watchlist" in row.result  # digest persisted
