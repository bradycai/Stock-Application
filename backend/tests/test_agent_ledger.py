"""Agent task ledger (tools/agent_ledger.py): dedup, retries, pause, file scopes, rate limit."""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[2] / "tools" / "agent_ledger.py"
_spec = importlib.util.spec_from_file_location("agent_ledger", _PATH)
ledger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ledger)

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def _claim(state, issue, files=(), now=T0, **kw):
    kw.setdefault("agent", "backend-data")
    kw.setdefault("branch", f"agent/{issue}-x")
    return ledger.claim(state, issue, files=list(files), now=now, **kw)


@pytest.fixture()
def path(tmp_path):
    return str(tmp_path / ".progress" / "ledger.json")


def run(path, *args):
    return ledger.main(["--ledger", path, *args])


def _bytes(path):
    return pathlib.Path(path).read_bytes()


# --------------------------------------------------------------------------- #
# claim                                                                       #
# --------------------------------------------------------------------------- #
def test_claim_records_every_field():
    st = ledger.empty_state()
    rec = _claim(st, 39, files=["tools/agent_ledger.py"], actor="bradycai", event="issue_comment",
                 budget_usd=5)
    assert rec == {
        "issue": 39, "actor": "bradycai", "event": "issue_comment", "agent": "backend-data",
        "branch": "agent/39-x", "pr": None, "state": "claimed", "retries": 0, "budget_usd": 5.0,
        "spent_usd": 0.0, "commit": None, "files": ["tools/agent_ledger.py"], "note": "",
        "outcome": None, "claimed_at": T0.isoformat(), "updated_at": T0.isoformat(), "released_at": None,
    }
    assert st["claims"]["39"] is rec and st["claim_log"] == [T0.isoformat()]


def test_duplicate_active_claim_is_refused():
    st = ledger.empty_state()
    _claim(st, 39)
    for state in ("working", "review", "blocked"):
        ledger.update(st, 39, state=state)
        with pytest.raises(ledger.Refused, match="already claimed"):
            _claim(st, 39, agent="frontend")
    assert st["claims"]["39"]["agent"] == "backend-data"


def test_reclaim_after_release_keeps_history():
    st = ledger.empty_state()
    _claim(st, 39)
    ledger.attempt(st, 39)
    ledger.release(st, 39, outcome="abandoned", note="wrong approach")
    rec = _claim(st, 39, agent="frontend", now=T0 + timedelta(minutes=5))
    assert rec["retries"] == 0 and rec["agent"] == "frontend"
    assert st["history"][-1]["outcome"] == "abandoned" and st["history"][-1]["retries"] == 1


@pytest.mark.parametrize("kw,why", [
    ({"budget_usd": -1}, "negative"),
])
def test_claim_rejects_bad_values(kw, why):
    with pytest.raises(ledger.Refused, match=why):
        _claim(ledger.empty_state(), 39, **kw)
    with pytest.raises(ledger.Refused, match="invalid issue"):
        _claim(ledger.empty_state(), 0)


# --------------------------------------------------------------------------- #
# attempt: retries and budget                                                 #
# --------------------------------------------------------------------------- #
def test_attempt_refuses_past_max_retries():
    st = ledger.empty_state()
    _claim(st, 39)
    for n in (1, 2, 3):
        assert ledger.attempt(st, 39)["retries"] == n
    with pytest.raises(ledger.Refused, match="3 of 3 retries"):
        ledger.attempt(st, 39)
    assert st["claims"]["39"]["retries"] == 3


def test_attempt_honours_a_custom_limit():
    st = ledger.empty_state()
    _claim(st, 39)
    ledger.attempt(st, 39, max_retries=1)
    with pytest.raises(ledger.Refused, match="1 of 1"):
        ledger.attempt(st, 39, max_retries=1)
    with pytest.raises(ledger.Refused):
        ledger.attempt(st, 39, max_retries=0)


def test_attempt_needs_an_active_claim():
    st = ledger.empty_state()
    with pytest.raises(ledger.Refused, match="no active claim"):
        ledger.attempt(st, 39)
    _claim(st, 39)
    ledger.release(st, 39)
    with pytest.raises(ledger.Refused, match="no active claim"):
        ledger.attempt(st, 39)


def test_attempt_refuses_once_the_budget_estimate_is_spent():
    st = ledger.empty_state()
    _claim(st, 39, budget_usd=2.0)
    ledger.update(st, 39, spent_usd=1.99)
    ledger.attempt(st, 39)
    ledger.update(st, 39, spent_usd=2.0)
    with pytest.raises(ledger.Refused, match="budget"):
        ledger.attempt(st, 39)


def test_no_budget_means_no_budget_refusal():
    st = ledger.empty_state()
    _claim(st, 39)
    ledger.update(st, 39, spent_usd=50.0)
    assert ledger.attempt(st, 39)["retries"] == 1


# --------------------------------------------------------------------------- #
# pause                                                                       #
# --------------------------------------------------------------------------- #
def test_pause_refuses_claims_and_attempts_but_allows_wind_down():
    st = ledger.empty_state()
    _claim(st, 39)
    ledger.pause(st, "cost spike", now=T0)
    with pytest.raises(ledger.Refused, match="paused .cost spike."):
        _claim(st, 40)
    with pytest.raises(ledger.Refused, match="paused"):
        ledger.attempt(st, 39)
    ledger.update(st, 39, state="blocked", note="waiting for resume")
    ledger.release(st, 39, outcome="abandoned")
    with pytest.raises(ledger.Refused, match="paused"):
        _claim(st, 39)
    ledger.resume(st)
    assert _claim(st, 40)["state"] == "claimed"


# --------------------------------------------------------------------------- #
# file scopes                                                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("a,b,overlap", [
    ("tools/agent_ledger.py", "tools/agent_ledger.py", True),
    ("tools/agent_ledger.py", "tools/maintenance_report.py", False),
    ("tools/*.py", "tools/agent_ledger.py", True),
    ("tools/*.py", "tools/sub/x.py", False),          # `*` stays inside one segment
    ("tools/**", "tools/sub/x.py", True),
    ("**/*.py", "frontend/src/App.jsx", False),
    ("**/*.py", "backend/app/main.py", True),
    ("**/*.py", "frontend/src/**", True),             # frontend/src/x.py could exist
    ("tools/*", "tools/sub/x.py", True),              # `tools/*` matches the directory `tools/sub`
    (".github", ".github/workflows/claude.yml", True),  # dot-directories are directories
    (".github/workflows/claude.yml", ".github/workflows/maintenance.yml", False),
    ("CLAUDE.md", "CLAUDE.md", True),
    ("**", "anything/at/all", True),
    ("frontend/src", "frontend/src/App.jsx", True),   # a directory owns what's below it
    ("frontend/src/", "frontend/src/pages/X.jsx", True),
    ("frontend/src/**", "frontend/package.json", False),
    ("backend/app/services/*.py", "backend/app/routers/*.py", False),
    ("backend/app/*/risk.py", "backend/app/services/*.py", True),
    ("backend/tests/test_a*.py", "backend/tests/test_b*.py", False),
    ("backend/tests/test_*.py", "backend/tests/*_ledger.py", True),
    ("backend/app/models.py", "backend/app/main.py", False),
    ("backend/app/[mn]*.py", "backend/app/models.py", True),
    ("backend/**/models.py", "backend/app/**", True),
    ("frontend/**", "backend/**", False),
])
def test_globs_overlap(a, b, overlap):
    assert ledger.globs_overlap(a, b) is overlap
    assert ledger.globs_overlap(b, a) is overlap


def test_parse_globs_normalizes():
    globs = ledger.parse_globs(" ./tools/x.py, /frontend/src/ ,,tools/x.py")
    assert globs == ["tools/x.py", "frontend/src/**"]
    assert ledger.parse_globs("") == [] and ledger.parse_globs(None) == []


def test_overlapping_file_scope_is_refused():
    st = ledger.empty_state()
    _claim(st, 39, files=["tools/agent_ledger.py", "backend/tests/test_agent_ledger.py"])
    why = r"overlaps the active claim on issue 39: tools/\*\* ~ tools/agent"
    with pytest.raises(ledger.Refused, match=why):
        _claim(st, 40, files=["frontend/src/**", "tools/**"])
    assert "40" not in st["claims"]
    assert _claim(st, 40, files=["frontend/src/**", "tools/maintenance_report.py"])["state"] == "claimed"


def test_released_claims_free_their_files():
    st = ledger.empty_state()
    _claim(st, 39, files=["tools/**"])
    ledger.release(st, 39)
    assert _claim(st, 40, files=["tools/agent_ledger.py"])["state"] == "claimed"


def test_claims_without_files_never_overlap():
    st = ledger.empty_state()
    _claim(st, 39)
    assert _claim(st, 40, files=["**"])["state"] == "claimed"


# --------------------------------------------------------------------------- #
# rate limit                                                                  #
# --------------------------------------------------------------------------- #
def test_more_than_n_claims_per_hour_is_refused():
    st = ledger.empty_state()
    for i in range(3):
        _claim(st, 100 + i, now=T0 + timedelta(minutes=i), max_per_hour=3)
    with pytest.raises(ledger.Refused, match="rate limit: 3 claims in the last hour"):
        _claim(st, 200, now=T0 + timedelta(minutes=30), max_per_hour=3)
    # Releasing does not refund the rate limit; only time does.
    ledger.release(st, 100)
    with pytest.raises(ledger.Refused, match="rate limit"):
        _claim(st, 200, now=T0 + timedelta(minutes=59), max_per_hour=3)
    assert _claim(st, 200, now=T0 + timedelta(minutes=60, seconds=1), max_per_hour=3)
    assert len(st["claim_log"]) == 3  # entries older than an hour are pruned


def test_unreadable_rate_log_entries_count_against_the_limit():
    st = ledger.empty_state()
    st["claim_log"] = ["garbage"]
    with pytest.raises(ledger.Refused, match="rate limit"):
        _claim(st, 39, max_per_hour=1)


# --------------------------------------------------------------------------- #
# update / release                                                            #
# --------------------------------------------------------------------------- #
def test_update_and_release():
    st = ledger.empty_state()
    _claim(st, 39)
    rec = ledger.update(st, 39, state="review", pr=57, commit="abc1234", spent_usd=0.8,
                        now=T0 + timedelta(minutes=1))
    assert (rec["state"], rec["pr"], rec["commit"], rec["spent_usd"]) == ("review", 57, "abc1234", 0.8)
    rec = ledger.release(st, 39, outcome="integrated", note="merged", now=T0 + timedelta(minutes=2))
    assert rec["state"] == "released" and rec["outcome"] == "integrated" and rec["note"] == "merged"


@pytest.mark.parametrize("fields,why", [
    ({"state": "released"}, "use `release`"),
    ({"state": "bogus"}, "state must be"),
    ({"spent_usd": -1.0}, "negative"),
    ({"budget_usd": -1.0}, "negative"),
])
def test_update_rejects_bad_values(fields, why):
    st = ledger.empty_state()
    _claim(st, 39)
    with pytest.raises(ledger.Refused, match=why):
        ledger.update(st, 39, **fields)
    assert st["claims"]["39"]["state"] == "claimed"


def test_update_and_release_need_an_active_claim():
    st = ledger.empty_state()
    with pytest.raises(ledger.Refused, match="no active claim"):
        ledger.update(st, 39, note="x")
    with pytest.raises(ledger.Refused, match="no active claim"):
        ledger.release(st, 39)
    _claim(st, 39)
    with pytest.raises(ledger.Refused, match="outcome"):
        ledger.release(st, 39, outcome="merged")
    ledger.release(st, 39)
    with pytest.raises(ledger.Refused, match="no active claim"):
        ledger.release(st, 39)


# --------------------------------------------------------------------------- #
# CLI and storage                                                             #
# --------------------------------------------------------------------------- #
def test_cli_round_trip(path, capsys):
    assert run(path, "claim", "39", "--agent", "general", "--branch", "agent/39-dev-automation",
               "--files", "tools/agent_ledger.py,.github/workflows/claude.yml", "--budget", "4",
               "--actor", "bradycai", "--event", "manual") == 0
    assert run(path, "attempt", "39") == 0
    assert run(path, "update", "39", "--state", "review", "--pr", "57", "--commit", "deadbeef",
               "--spent", "1.5") == 0
    capsys.readouterr()
    assert run(path, "status") == 0
    out = capsys.readouterr().out
    assert "#39" in out and "REVIEW" in out and "PR #57" in out and "retries 1" in out
    assert "budget $4.00 (spent $1.50)" in out and "tools/agent_ledger.py" in out
    assert run(path, "status", "39", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["claims"]["39"]["commit"] == "deadbeef" and data["paused"] is False
    assert run(path, "release", "39", "--note", "integrated after review") == 0
    assert run(path, "status") == 0
    assert "(no matching claims)" in capsys.readouterr().out
    assert run(path, "status", "--all") == 0
    assert "RELEASED" in capsys.readouterr().out


@pytest.mark.parametrize("setup,args,why", [
    ([["claim", "39", "--agent", "a", "--branch", "b"]],
     ["claim", "39", "--agent", "a", "--branch", "b"], "already claimed"),
    ([["claim", "39", "--agent", "a", "--branch", "b", "--files", "tools/**"]],
     ["claim", "40", "--agent", "a", "--branch", "c", "--files", "tools/x.py"], "overlaps"),
    ([["claim", "39", "--agent", "a", "--branch", "b"], ["attempt", "39", "--max-retries", "1"]],
     ["attempt", "39", "--max-retries", "1"], "retries"),
    ([["claim", "39", "--agent", "a", "--branch", "b"], ["pause", "--reason", "stop"]],
     ["claim", "40", "--agent", "a", "--branch", "c"], "paused"),
    ([["claim", "39", "--agent", "a", "--branch", "b"], ["pause"]],
     ["attempt", "39"], "paused"),
    ([["claim", "39", "--agent", "a", "--branch", "b", "--max-per-hour", "1"]],
     ["claim", "40", "--agent", "a", "--branch", "c", "--max-per-hour", "1"], "rate limit"),
    ([], ["release", "39"], "no active claim"),
])
def test_cli_refusals_exit_1_and_write_nothing(path, capsys, setup, args, why):
    for step in setup:
        assert run(path, *step) == 0
    before = _bytes(path) if os.path.exists(path) else None
    assert run(path, *args) == 1
    assert why in capsys.readouterr().err
    assert (_bytes(path) if os.path.exists(path) else None) == before


def test_pause_and_resume_via_cli(path, capsys):
    assert run(path, "pause", "--reason", "weekend") == 0
    assert run(path, "status") == 0
    assert "PAUSED" in capsys.readouterr().out
    assert run(path, "claim", "39", "--agent", "a", "--branch", "b") == 1
    assert run(path, "resume") == 0
    assert run(path, "claim", "39", "--agent", "a", "--branch", "b") == 0


def test_negative_money_is_a_usage_error(path):
    with pytest.raises(SystemExit):
        run(path, "claim", "39", "--agent", "a", "--branch", "b", "--budget", "-3")


@pytest.mark.parametrize("content", ["{not json", "[]", '{"claims": []}'])
def test_unreadable_ledger_fails_closed_and_is_never_overwritten(path, capsys, content):
    os.makedirs(os.path.dirname(path))
    pathlib.Path(path).write_text(content)
    assert run(path, "claim", "39", "--agent", "a", "--branch", "b") == 1
    assert run(path, "status") == 1
    assert "ERROR" in capsys.readouterr().err
    assert pathlib.Path(path).read_text() == content
    with pytest.raises(ledger.LedgerError):
        ledger.load(path)


def test_writes_are_atomic_via_a_per_process_temp_file(path, monkeypatch):
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(ledger.os, "replace", spy)
    assert run(path, "claim", "39", "--agent", "a", "--branch", "b") == 0
    assert calls == [(f"{path}.{os.getpid()}.tmp", path)]
    leftovers = [p for p in os.listdir(os.path.dirname(path)) if p.endswith(".tmp")]
    assert leftovers == []


def test_concurrent_claims_on_the_same_issue_admit_exactly_one(path):
    os.makedirs(os.path.dirname(path))
    cmd = [sys.executable, str(_PATH), "--ledger", path, "claim", "39", "--agent", "a", "--branch", "b"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: subprocess.run(cmd, capture_output=True, text=True), range(8)))
    assert sorted(r.returncode for r in results) == [0] + [1] * 7
    assert sum("already claimed" in r.stderr for r in results) == 7
    assert list(ledger.load(path)["claims"]) == ["39"]


def test_default_ledger_lives_in_the_gitignored_progress_dir():
    assert ledger.DEFAULT_LEDGER.endswith(os.path.join(".progress", "ledger.json"))
    gitignore = (_PATH.parents[1] / ".gitignore").read_text().splitlines()
    assert ".progress/" in gitignore
