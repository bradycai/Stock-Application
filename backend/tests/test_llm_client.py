"""The central Claude client: routing, request shape, fail-closed handling, accounting.
Uses a fake Anthropic client — no network."""
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from pydantic import BaseModel, Field

from app.config import settings
from app.services import llm


class Out(BaseModel):
    ticker: str
    score: float = Field(ge=0, le=1)


def _resp(text='{"ticker": "AAPL", "score": 0.5}', stop_reason="end_turn", model="claude-opus-5-5",
          usage=None, stop_details=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason, stop_details=stop_details, model=model, _request_id="req_1",
        usage=usage or SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                                       cache_creation_input_tokens=0, iterations=None),
    )


class FakeClient:
    def __init__(self, response=None, exc=None):
        self.calls = []
        self._response, self._exc = response, exc
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc:
            raise self._exc
        return self._response


@pytest.fixture()
def ledger(monkeypatch):
    rows = []
    monkeypatch.setattr(llm, "_record_call", lambda meta: rows.append(dict(meta)))
    return rows


@pytest.fixture()
def roles(monkeypatch):
    monkeypatch.setattr(settings, "strategist_model", "claude-opus-5-5")
    monkeypatch.setattr(settings, "strategist_effort", "high")
    monkeypatch.setattr(settings, "position_model", "")
    monkeypatch.setattr(settings, "watcher_model", "claude-haiku-5-5")
    monkeypatch.setattr(settings, "watcher_effort", "low")
    monkeypatch.setattr(settings, "llm_refusal_fallbacks", True)


def _call(monkeypatch, client, role="strategist"):
    monkeypatch.setattr(llm, "_get_client", lambda: client)
    return llm.call_structured(role=role, prompt_name="test", system="SYS", user="USER", output_model=Out)


def test_request_shape_for_opus_strategist(monkeypatch, ledger, roles):
    client = FakeClient(_resp())
    result = _call(monkeypatch, client)
    req = client.calls[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"]["effort"] == "high"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["thinking"] == {"type": "adaptive"}
    assert req["fallbacks"] == "default" and req["betas"] == [llm.FALLBACK_BETA]
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in req and "tools" not in req
    assert result.output.ticker == "AAPL"
    assert ledger[-1]["status"] == "ok" and ledger[-1]["prompt_version"].startswith("test@")


def test_haiku_watcher_never_sends_fallbacks(monkeypatch, ledger, roles):
    client = FakeClient(_resp(model="claude-haiku-5-5"))
    _call(monkeypatch, client, role="watcher")
    req = client.calls[0]
    assert req["model"] == "claude-haiku-5-5" and req["output_config"]["effort"] == "low"
    assert "fallbacks" not in req and "betas" not in req


def test_position_role_defaults_to_strategist_model(roles):
    assert llm.get_role("position").model == "claude-opus-5-5"


def test_invalid_effort_is_rejected(monkeypatch, roles):
    monkeypatch.setattr(settings, "strategist_effort", "extreme")
    with pytest.raises(ValueError):
        llm.get_role("strategist")


def test_refusal_fails_closed_with_category(monkeypatch, ledger, roles):
    client = FakeClient(_resp(text="", stop_reason="refusal", stop_details=SimpleNamespace(category="cyber")))
    with pytest.raises(llm.LLMError) as e:
        _call(monkeypatch, client)
    assert e.value.kind == "refusal" and e.value.meta["refusal_category"] == "cyber"
    assert ledger[-1]["status"] == "refusal"


def test_truncated_response_fails_closed(monkeypatch, ledger, roles):
    client = FakeClient(_resp(text='{"ticker": "AA', stop_reason="max_tokens"))
    with pytest.raises(llm.LLMError) as e:
        _call(monkeypatch, client)
    assert e.value.kind == "incomplete"
    assert ledger[-1]["status"] == "incomplete"


@pytest.mark.parametrize("text", ['{"ticker": "AAPL", "score": 1.7}', "not json", '{"ticker": "AAPL"}'])
def test_invalid_output_fails_closed(monkeypatch, ledger, roles, text):
    with pytest.raises(llm.LLMError) as e:
        _call(monkeypatch, FakeClient(_resp(text=text)))
    assert e.value.kind == "invalid_output"


def test_api_error_fails_closed(monkeypatch, ledger, roles):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(529, request=request, json={"error": {"message": "overloaded"}})
    exc = anthropic.APIStatusError("overloaded", response=response, body=None)
    with pytest.raises(llm.LLMError) as e:
        _call(monkeypatch, FakeClient(exc=exc))
    assert e.value.kind == "api_error" and "529" in e.value.detail
    assert ledger[-1]["status"] == "api_error"


def test_usage_cost_and_fallback_are_recorded(monkeypatch, ledger, roles):
    usage = SimpleNamespace(input_tokens=2000, output_tokens=500, cache_read_input_tokens=1000,
                            cache_creation_input_tokens=0,
                            iterations=[SimpleNamespace(type="message"),
                                        SimpleNamespace(type="fallback_message")])
    result = _call(monkeypatch, FakeClient(_resp(model="claude-opus-4-8", usage=usage)))
    meta = result.meta
    assert meta["served_model"] == "claude-opus-4-8" and meta["fallback_used"] is True
    # priced at the model that actually served the request
    assert meta["cost_usd"] == llm.estimate_cost("claude-opus-4-8", 2000, 500, 1000, 0)
    assert json.dumps(ledger[-1], default=str)  # serializable for the ledger/log


def test_estimate_cost():
    # Opus 5.5: $4 in, $20 out, cache reads at 0.05x, cache writes at 1.25x (per MTok)
    assert llm.estimate_cost("claude-opus-5-5", 1_000_000, 0) == 4.0
    assert llm.estimate_cost("claude-opus-5-5", 0, 1_000_000) == 20.0
    assert llm.estimate_cost("claude-opus-5-5", 0, 0, cache_read_tokens=1_000_000) == 0.2
    assert llm.estimate_cost("claude-opus-5-5", 0, 0, cache_write_tokens=1_000_000) == 5.0
    # Haiku 5.5 switches rate card above 100K prompt tokens
    assert llm.estimate_cost("claude-haiku-5-5", 100_000, 0) == 0.01
    assert llm.estimate_cost("claude-haiku-5-5", 200_000, 0) == 0.1
    assert llm.estimate_cost("unknown-model", 10, 10) is None


def test_prompt_version_tracks_prompt_text():
    a = llm.prompt_version("p", "text one")
    assert a == llm.prompt_version("p", "text one")
    assert a != llm.prompt_version("p", "text two")
