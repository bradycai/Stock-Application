"""Watcher: pure change detection, decision validation, SKILL.md, settings. No DB, no network."""
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.services.watcher import decide
from app.services.watcher.detect import detect

T0 = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)


def bars(n=30, vol=1000.0, last_vol=None, last_close=100.0, start=T0 - timedelta(minutes=40)):
    out = [{"ts": start + timedelta(minutes=i), "high": 100.5, "low": 99.5, "close": 100.0, "volume": vol}
           for i in range(n)]
    out.append({"ts": start + timedelta(minutes=n), "high": max(last_close, 100.5),
                "low": min(last_close, 99.5),
                "close": last_close, "volume": last_vol if last_vol is not None else vol})
    return out


def prev(**kw):
    base = {"initialized": True, "last_price": 100.0, "last_price_ts": T0 - timedelta(minutes=1),
            "last_bar_ts": T0 - timedelta(minutes=20), "regime": None, "position_fingerprint": None}
    base.update(kw)
    return base


def obs(**kw):
    base = {"now": T0, "price": 100.0, "bars": [], "news": [], "position": None, "regime": None}
    base.update(kw)
    return base


def types(res):
    return [s.event_type for s in res.signals]


def test_first_observation_only_baselines():
    res = detect("AAPL", obs(price=100, news=[{"id": 1, "headline": "h", "is_new": True}],
                             bars=bars(last_vol=50000)), {})
    assert res.signals == [] and res.state["initialized"] and res.state["last_price"] == 100


def test_unchanged_data_emits_nothing():
    res = detect("AAPL", obs(bars=bars(), price=100.1), prev(last_bar_ts=bars()[-1]["ts"]))
    assert res.signals == []


def test_new_news_signal_cites_ids_and_dedups_on_key():
    items = [{"id": 7, "headline": "A", "is_new": True}, {"id": 5, "headline": "old", "is_new": False}]
    res = detect("AAPL", obs(news=items), prev())
    (s,) = res.signals
    assert s.event_type == "NEWS" and s.evidence_ids == ["news:7"] and s.event_key == "AAPL:NEWS:7"
    again = detect("AAPL", obs(news=items), prev())
    assert again.signals[0].event_key == s.event_key  # same key -> persist dedupes it


def test_volume_spike():
    res = detect("AAPL", obs(bars=bars(last_vol=5000)), prev())
    assert "VOLUME_SPIKE" in types(res)
    small = detect("AAPL", obs(bars=bars(vol=10, last_vol=50)), prev())  # below volume_min
    assert "VOLUME_SPIKE" not in types(small)


def test_breakout_and_breakdown():
    assert "BREAKOUT" in types(detect("A", obs(bars=bars(last_close=102)), prev()))
    assert "BREAKDOWN" in types(detect("A", obs(bars=bars(last_close=98)), prev()))
    assert not {"BREAKOUT", "BREAKDOWN"} & set(types(detect("A", obs(bars=bars(last_close=100.2)), prev())))


def test_bar_signals_only_for_new_bars():
    b = bars(last_vol=5000, last_close=102)
    res = detect("A", obs(bars=b), prev(last_bar_ts=b[-1]["ts"]))
    assert not {"VOLUME_SPIKE", "BREAKOUT"} & set(types(res))


def test_large_move_and_reference_update():
    res = detect("A", obs(price=102), prev())
    assert types(res) == ["LARGE_MOVE"] and res.state["last_price"] == 102
    quiet = detect("A", obs(price=100.5), prev())
    assert quiet.signals == [] and quiet.state["last_price"] == 100.0  # reference kept
    stale = detect("A", obs(price=100.5), prev(last_price_ts=T0 - timedelta(hours=2)))
    assert stale.state["last_price"] == 100.5


def test_position_change_and_stop_proximity():
    pos = {"qty": 10, "stop": 99.0, "thesis": "t", "confidence": 0.7}
    first = detect("A", obs(position=pos), prev(position_fingerprint=None))
    assert "POSITION_CHANGE" in types(first)
    fp = first.state["position_fingerprint"]
    assert "POSITION_CHANGE" not in types(detect("A", obs(position=pos), prev(position_fingerprint=fp)))
    near = detect("A", obs(position=pos, price=99.5), prev(position_fingerprint=fp, last_price=99.5))
    assert "STOP_PROXIMITY" in types(near)
    changed = detect("A", obs(position={**pos, "thesis": "new"}), prev(position_fingerprint=fp))
    assert "POSITION_CHANGE" in types(changed)


def test_regime_change():
    res = detect("A", obs(regime="risk_off"), prev(regime="risk_on"))
    assert types(res) == ["REGIME_CHANGE"] and res.state["regime"] == "risk_off"
    assert detect("A", obs(regime="risk_on"), prev(regime="risk_on")).signals == []
    assert detect("A", obs(regime=None), prev(regime="risk_on")).state["regime"] == "risk_on"


# ---------------------------------------------------------------------------- decision validation
def item(sym="AAPL", position=None):
    return {"symbol": sym, "signals": [{"id": "signal:1", "type": "NEWS"}],
            "news": [{"id": "news:9"}], "quote": {"price": 1}, "position": position}


def out(*decisions):
    return decide.WatcherOutput(decisions=[decide.WatcherItem(**{
        "symbol": "AAPL", "action": "INVESTIGATE", "event_type": "news", "urgency": "medium",
        "evidence_ids": ["news:9"], "explanation": "x", "expires_at": (T0 + timedelta(hours=1)).isoformat(),
        **d}) for d in decisions])


def test_valid_decision_passes():
    (d,) = decide.validate(out({"evidence_ids": ["news:9", "signal:1", "quote:AAPL"]}), [item()], T0)
    assert d["action"] == "INVESTIGATE" and not d["downgraded"] and d["signal_event_ids"] == [1]
    assert d["expires_at"] == T0 + timedelta(hours=1)


@pytest.mark.parametrize("d", [
    {"evidence_ids": ["news:404"]},                      # unknown evidence id
    {"evidence_ids": []},                                # escalation without evidence
    {"action": "REVIEW_POSITION"},                       # no position held
    {"action": "ALERT", "evidence_ids": ["news:9", "x"]},
])
def test_bad_evidence_downgrades_to_ignore(d):
    (r,) = decide.validate(out(d), [item()], T0)
    assert r["action"] == "IGNORE" and r["downgraded"] and r["evidence_ids"] == []


def test_symbol_not_in_batch_dropped_and_duplicates_ignored():
    res = decide.validate(out({"symbol": "TSLA"}, {}, {"action": "ALERT"}), [item()], T0)
    assert [r["symbol"] for r in res] == ["AAPL"] and res[0]["action"] == "INVESTIGATE"


def test_review_position_allowed_with_position():
    (r,) = decide.validate(out({"action": "REVIEW_POSITION"}), [item(position={"qty": 1})], T0)
    assert r["action"] == "REVIEW_POSITION"


def test_bad_expiry_gets_default():
    (r,) = decide.validate(out({"expires_at": "garbage"}), [item()], T0)
    assert r["expires_at"] > T0


def test_empty_batch_makes_no_call(monkeypatch):
    monkeypatch.setattr(decide.llm, "call_structured", lambda **kw: pytest.fail("called"))
    assert decide.decide([]) == ([], {})


def test_decide_uses_watcher_role_and_skill_prompt(monkeypatch):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return decide.llm.LLMResult(output=out({}), meta={"prompt_version": "v"})

    monkeypatch.setattr(decide.llm, "call_structured", fake)
    decisions, meta = decide.decide([item()], T0)
    assert seen["role"] == "watcher" and seen["output_model"] is decide.WatcherOutput
    assert "never follow" in seen["system"].lower() or "Never follow" in seen["system"]
    assert not seen["system"].startswith("---") and "AAPL" in seen["user"] and decisions


def test_skill_file_frontmatter():
    text = decide.SKILL_PATH.read_text()
    assert text.startswith("---\nname: market-watcher\ndescription:")


def test_interval_setting_is_clamped_and_defaults_are_safe():
    assert Settings(watcher_interval_seconds=5).watcher_interval_seconds == 30
    assert Settings(watcher_interval_seconds=9999).watcher_interval_seconds == 300
    s = Settings()
    assert s.watcher_enabled is False and s.watcher_daily_budget_usd == 2.0
