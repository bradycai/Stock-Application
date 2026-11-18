"""Auto-merge policy (tools/automerge_policy.py): merge only when everything is good."""
import importlib.util
import pathlib

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[2] / "tools" / "automerge_policy.py"
_spec = importlib.util.spec_from_file_location("automerge_policy", _PATH)
policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy)

PATTERNS = policy.load_protected_patterns()


def _pr(**over):
    pr = {
        "number": 1, "state": "OPEN", "isDraft": False, "isCrossRepository": False,
        "baseRefName": "Production", "labels": [{"name": "automerge"}], "reviewDecision": "",
        "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [
            {"name": "Backend (lint + tests)", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "Frontend (install + build)", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
    }
    pr.update(over)
    return pr


def _ok(pr, files=("frontend/src/pages/JournalPage.jsx",)):
    return policy.evaluate(pr, list(files), PATTERNS, "Production")


def test_everything_good_is_eligible():
    ok, reasons = _ok(_pr())
    assert ok and reasons == []


@pytest.mark.parametrize("over,why", [
    ({"labels": []}, "label"),
    ({"isDraft": True}, "draft"),
    ({"isCrossRepository": True}, "fork"),
    ({"state": "CLOSED"}, "state"),
    ({"baseRefName": "agent/integration-2"}, "retarget"),
    ({"reviewDecision": "CHANGES_REQUESTED"}, "requests changes"),
    ({"mergeable": "CONFLICTING"}, "not mergeable"),
    ({"mergeStateStatus": "BLOCKED"}, "BLOCKED"),
])
def test_any_failed_condition_blocks(over, why):
    ok, reasons = _ok(_pr(**over))
    assert not ok and any(why in r for r in reasons)


def test_failing_pending_or_missing_checks_block():
    failing = _pr(statusCheckRollup=[
        {"name": "Backend (lint + tests)", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"name": "Frontend (install + build)", "status": "COMPLETED", "conclusion": "SUCCESS"}])
    assert not _ok(failing)[0]
    pending = _pr(statusCheckRollup=[
        {"name": "Backend (lint + tests)", "status": "IN_PROGRESS", "conclusion": None},
        {"name": "Frontend (install + build)", "status": "COMPLETED", "conclusion": "SUCCESS"}])
    assert not _ok(pending)[0]
    missing = _pr(statusCheckRollup=[
        {"name": "Frontend (install + build)", "status": "COMPLETED", "conclusion": "SUCCESS"}])
    ok, reasons = _ok(missing)
    assert not ok and any("Backend" in r for r in reasons)


@pytest.mark.parametrize("path", [
    "backend/app/services/risk.py", "backend/app/services/execution.py",
    "backend/app/services/broker.py", "backend/app/services/monitor.py",
    "backend/app/config.py", "backend/app/models.py", "backend/migrations/versions/0009_x.py",
    "backend/.env.example", ".github/workflows/ci.yml", ".github/CODEOWNERS",
    "CLAUDE.md", ".claude/agents/qa-security.md", "tools/automerge_policy.py",
])
def test_protected_files_always_need_a_human(path):
    ok, reasons = _ok(_pr(), files=["frontend/src/App.jsx", path])
    assert not ok and any("CODEOWNERS-protected" in r for r in reasons)


def test_unprotected_changes_are_allowed():
    files = ["frontend/src/pages/UsagePage.jsx", "backend/app/routers/usage.py",
             "backend/tests/test_dashboards.py", "README.md"]
    assert _ok(_pr(), files=files)[0]


def test_policy_stays_protected_even_without_codeowners(tmp_path):
    patterns = policy.load_protected_patterns(str(tmp_path / "missing"))
    assert policy.is_protected("tools/automerge_policy.py", patterns)
    assert policy.is_protected(".github/workflows/auto-merge.yml", patterns)
