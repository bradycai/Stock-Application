"""analyst.py on top of the model client: batch restriction and fail-closed handling.
llm.call_structured is replaced with a fake — no network."""
from types import SimpleNamespace

import pytest

from app.schemas import AnalysisOutput, MonitorDecision, PositionDecision, TradeIntent
from app.services import analyst, llm


def _ti(ticker, action="watch"):
    return TradeIntent(ticker=ticker, action=action, order_type="limit", limit_price=0,
                       position_size_percent=0, stop_loss_percent=0, take_profit_percent=0,
                       confidence=0.5, reason="r" * 300, risks="k", evidence=[])


def _fake_call(output=None, error=None, calls=None):
    def call_structured(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        if error:
            raise error
        return llm.LLMResult(output=output, meta={"input_tokens": 10, "output_tokens": 5})
    return call_structured


def test_batch_is_restricted_to_its_tickers_and_reports_missing(monkeypatch):
    out = AnalysisOutput(intents=[_ti("AAPL"), _ti("TSLA")])  # TSLA was not requested
    calls = []
    monkeypatch.setattr(llm, "call_structured", _fake_call(out, calls=calls))
    output, meta, ok = analyst.analyze_batch({"watchlist_data": {}}, allowed_tickers={"AAPL", "MSFT"})
    assert ok is True
    assert [i.ticker for i in output.intents] == ["AAPL"]
    assert meta["dropped"] == ["TSLA"] and meta["missing"] == ["MSFT"]
    assert calls[0]["role"] == "strategist" and calls[0]["output_model"] is AnalysisOutput
    assert "['AAPL', 'MSFT']" in calls[0]["user"]
    assert len(output.intents[0].reason) == analyst.FIELD_LIMIT  # long text truncated


def test_batch_failure_returns_not_ok_and_no_intents(monkeypatch):
    err = llm.LLMError("refusal", "declined", {"status": "refusal"})
    monkeypatch.setattr(llm, "call_structured", _fake_call(error=err))
    output, meta, ok = analyst.analyze_batch({"watchlist_data": {"AAPL": {}}})
    assert ok is False and output.intents == [] and meta["error"].startswith("refusal")


def _monitor(ticker="AAPL"):
    return MonitorDecision(ticker=ticker, action="hold", confidence=0.6, reason="ok",
                           size_percent=0, stop_loss_percent=0, take_profit_percent=0, limit_price=0)


def test_monitor_decision_uses_position_role(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "call_structured", _fake_call(_monitor(), calls=calls))
    decision, _meta, ok = analyst.monitor_decision({"ticker": "AAPL"})
    assert ok and decision.action == "hold" and calls[0]["role"] == "position"


@pytest.mark.parametrize("fn,model", [
    (analyst.monitor_decision, lambda: _monitor("TSLA")),
    (analyst.position_review, lambda: PositionDecision(ticker="TSLA", action="sell", confidence=0.9,
                                                       reason="x", updated_thesis="", exit_condition="")),
])
def test_decision_for_a_different_ticker_fails_closed(monkeypatch, fn, model):
    monkeypatch.setattr(llm, "call_structured", _fake_call(model()))
    decision, meta, ok = fn({"ticker": "AAPL"})
    assert ok is False and decision is None and "ticker_mismatch" in meta["error"]


def test_filter_to_allowed_uppercases_and_drops():
    out = AnalysisOutput(intents=[_ti("AAPL"), _ti("GME")])
    kept, dropped = analyst.filter_to_allowed(out, {"aapl"})
    assert [i.ticker for i in kept.intents] == ["AAPL"] and dropped == ["GME"]


def test_schema_rejects_extra_fields_and_bad_tickers():
    with pytest.raises(Exception):
        TradeIntent.model_validate({**_ti("AAPL").model_dump(), "quantity": 100})
    with pytest.raises(Exception):
        TradeIntent.model_validate({**_ti("AAPL").model_dump(), "ticker": "aapl; DROP"})


def test_execution_rejects_expired_intents(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.config import settings
    from app.models import IntentStatus
    from app.services import execution

    monkeypatch.setattr(settings, "intent_max_age_min", 60)
    added = []
    db = SimpleNamespace(add=added.append)
    old = SimpleNamespace(id=1, ticker="AAPL", status=IntentStatus.pending_approval,
                          created_at=datetime.now(timezone.utc) - timedelta(minutes=61))
    with pytest.raises(execution.ExecutionError) as e:
        execution.expire_if_stale(db, old)
    assert e.value.status_code == 409 and old.status == IntentStatus.canceled
    assert added[0].event_type == "intent_expired"

    fresh = SimpleNamespace(id=2, ticker="AAPL", status=IntentStatus.pending_approval,
                            created_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    execution.expire_if_stale(db, fresh)  # no exception
    assert fresh.status == IntentStatus.pending_approval
