"""Maintenance triage (tools/maintenance_report.py): Dependabot classification and the report.

Pure functions only: nothing here calls `gh` or the GitHub API.
"""
import importlib.util
import pathlib
from datetime import datetime, timezone

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[2] / "tools" / "maintenance_report.py"
_spec = importlib.util.spec_from_file_location("maintenance_report", _PATH)
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_github(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("tests must not call gh / the GitHub API")
    monkeypatch.setattr(report, "_gh", refuse)
    monkeypatch.setattr(report.subprocess, "run", refuse)


# --------------------------------------------------------------------------- #
# classify_dependabot                                                         #
# --------------------------------------------------------------------------- #
CASES = [
    # (title, branch, labels, expected category, reason fragment)
    ("Bump fastapi from 0.115.0 to 0.115.6 in /backend", "dependabot/pip/backend/fastapi-0.115.6",
     ["dependencies", "python"], "minor-patch", "minor/patch 0.115.0 -> 0.115.6"),
    ("Bump httpx from 0.27.0 to 0.27.2 in /backend", "dependabot/pip/backend/httpx-0.27.2",
     ["dependencies"], "minor-patch", "minor/patch 0.27.0 -> 0.27.2"),
    ("Bump pydantic from 2.9.1 to 2.10.0 in /backend", "dependabot/pip/backend/pydantic-2.10.0",
     [], "minor-patch", "minor/patch"),
    ("Bump vite from 6.0.1 to 6.0.7 in /frontend", "dependabot/npm_and_yarn/frontend/vite-6.0.7",
     ["javascript"], "minor-patch", "minor/patch"),
    ("Bump react from 18.3.1 to 19.0.0 in /frontend", "dependabot/npm_and_yarn/frontend/react-19.0.0",
     [], "major", "major version 18.3.1 -> 19.0.0"),
    ("Bump @vitejs/plugin-react from 4.3.1 to 4.3.4 in /frontend",
     "dependabot/npm_and_yarn/frontend/vitejs/plugin-react-4.3.4", [], "minor-patch", "minor/patch"),
    ("Bump uvicorn from 0.30.0 to 0.32.0 in /backend", "dependabot/pip/backend/uvicorn-0.32.0",
     [], "major", "major version 0.30.0 -> 0.32.0"),  # 0.x: a minor bump may break
    # high-risk packages, any version change
    ("Bump anthropic from 0.40.0 to 0.40.1 in /backend", "dependabot/pip/backend/anthropic-0.40.1",
     [], "high-risk", "high-risk package: anthropic"),
    ("Bump alpaca-py from 0.33.0 to 0.33.1 in /backend", "dependabot/pip/backend/alpaca-py-0.33.1",
     [], "high-risk", "alpaca-py"),
    ("Bump sqlalchemy from 2.0.35 to 2.0.36 in /backend", "dependabot/pip/backend/sqlalchemy-2.0.36",
     [], "high-risk", "sqlalchemy"),
    ("Bump SQLAlchemy from 2.0.35 to 2.0.36 in /backend", "", [], "high-risk", "sqlalchemy"),
    ("Bump psycopg[binary] from 3.2.1 to 3.2.3 in /backend", "dependabot/pip/backend/psycopg-binary--3.2.3",
     [], "high-risk", "psycopg"),
    ("Bump psycopg2-binary from 2.9.9 to 2.9.10", "", [], "high-risk", "psycopg2-binary"),
    ("Update alembic requirement from <1.14 to <1.15 in /backend", "dependabot/pip/backend/alembic-1.14.0",
     [], "high-risk", "alembic"),
    ("Bump anthropic from 0.40.0 to 1.0.0 in /backend", "", [], "high-risk", "major version 0.40.0 -> 1.0.0"),
    ("Bump @anthropic-ai/sdk from 0.30.0 to 0.30.1 in /frontend", "", [], "high-risk", "anthropic"),
    # GitHub Actions: by branch, by label, or by the owner/name title alone
    ("Bump actions/checkout from 4 to 5", "dependabot/github_actions/actions/checkout-5",
     ["github_actions"], "high-risk", "GitHub Actions update"),
    ("Bump actions/setup-python from 5.1.0 to 5.2.0", "", [], "high-risk", "GitHub Actions update"),
    ("Bump anthropics/claude-code-action from 1 to 2", "", [], "high-risk", "major version 1 -> 2"),
    ("Bump actions/checkout from 11bd719 to 692973e", "dependabot/github_actions/actions/checkout-692973e",
     [], "high-risk", "could not read the version change"),
    # security wins over everything
    ("Bump requests from 2.31.0 to 2.32.0 in /backend", "dependabot/pip/backend/requests-2.32.0",
     ["dependencies", "security"], "security", "security fix"),
    ("[Security] Bump jinja2 from 3.1.3 to 3.1.4", "", [], "security", "security fix"),
    ("Bump sqlalchemy from 2.0.0 to 2.0.1", "", ["Security"], "security", "high-risk package: sqlalchemy"),
    ("Bump undici from 5.0.0 to 6.0.0", "dependabot/npm_and_yarn/security-fix", [], "security", "major"),
    # groups
    ("Bump the backend-minor-patches group in /backend with 5 updates",
     "dependabot/pip/backend/backend-minor-patches-1a2b3c4d5e", [], "minor-patch", "minor and patch"),
    ("Bump the frontend-minor-patches group across 1 directory with 3 updates", "", [], "minor-patch",
     "group frontend-minor-patches"),
    ("Bump the npm group in /frontend with 2 updates", "", [], "major", "update types unknown"),
    # unreadable: fail closed (never classified as routine)
    ("Bump something", "dependabot/pip/backend/something", [], "major", "could not read"),
    ("", "", [], "major", "could not read"),
    ("Bump foo from git+https://example.com/x to 1.0.0", "", [], "major", "could not read"),
]


@pytest.mark.parametrize("title,branch,labels,category,why", CASES)
def test_classify_dependabot(title, branch, labels, category, why):
    got, reason = report.classify_dependabot(title, branch, labels)
    assert got == category, reason
    assert why.lower() in reason.lower()


def test_group_with_a_high_risk_package_in_the_summary_is_high_risk():
    title = "Bump the backend-minor-patches group in /backend with 2 updates"
    summary = ("Bumps the backend-minor-patches group with 2 updates in the /backend directory: "
               "[fastapi](https://github.com/fastapi/fastapi) and [sqlalchemy](https://github.com/sqlalchemy).")
    assert report.classify_dependabot(title, "", [], summary)[0] == "high-risk"
    assert report.classify_dependabot(title, "", [])[0] == "minor-patch"


@pytest.mark.parametrize("name,risky", [
    ("anthropical", False), ("my-alembica", False), ("xalpaca", False), ("notsqlalchemyx", False),
    ("alpaca-trade-api", True),    # any Alpaca package is broker code
    ("flask-sqlalchemy", True),    # an SQLAlchemy plugin: conservative
    ("psycopg-pool", True),
])
def test_high_risk_names_match_whole_names_only(name, risky):
    category, _ = report.classify_dependabot(f"Bump {name} from 1.0.0 to 1.0.1", "", [])
    assert category == ("high-risk" if risky else "minor-patch")


def test_every_category_is_ordered_and_only_minor_patch_is_routine():
    assert report.CATEGORIES == ("security", "high-risk", "major", "minor-patch")
    assert set(report.NEEDS_HUMAN) == {"security", "high-risk", "major"}


@pytest.mark.parametrize("old,new,major", [
    ((1, 2, 3), (1, 3, 0), False),
    ((1, 2, 3), (2, 0, 0), True),
    ((0, 4, 1), (0, 4, 9), False),
    ((0, 4, 1), (0, 5, 0), True),
    ((0,), (1,), True),
    ((4,), (4, 1), False),
])
def test_is_major_bump(old, new, major):
    assert report.is_major_bump(old, new) is major


@pytest.mark.parametrize("title,change", [
    ("Bump x from 1.2.3 to 1.2.4", ((1, 2, 3), (1, 2, 4))),
    ("Update x requirement from >=2.0,<3 to >=2.0,<4 in /backend", ((3,), (4,))),
    ("Update x requirement from ==1.13.1 to ==1.14.0", ((1, 13, 1), (1, 14, 0))),
    ("Bump x from v4 to v5", ((4,), (5,))),
    ("Bump x from 2.0.0rc1 to 2.0.0", ((2, 0, 0), (2, 0, 0))),
    ("Bump x from 1.0.0 to latest", None),
    ("Bump the group with 3 updates", None),
])
def test_version_change(title, change):
    assert report.version_change(title) == change


# --------------------------------------------------------------------------- #
# CI state, Dependabot detection, age                                         #
# --------------------------------------------------------------------------- #
def _run(wf, n, status="completed", conclusion="success"):
    return {"workflow_id": wf, "run_number": n, "status": status, "conclusion": conclusion}


@pytest.mark.parametrize("runs,state", [
    ([], "no runs"),
    ([_run(1, 1), _run(2, 1, conclusion="skipped")], "passing"),
    ([_run(1, 1), _run(2, 1, conclusion="failure")], "failing"),
    ([_run(1, 1, conclusion="failure"), _run(1, 2)], "passing"),   # a re-run fixed it
    ([_run(1, 2, conclusion="failure"), _run(1, 1)], "failing"),
    ([_run(1, 1, status="in_progress", conclusion=None)], "pending"),
    ([_run(1, 1, conclusion="cancelled")], "failing"),
    ([_run(1, 1, conclusion="failure"), _run(2, 1, status="queued", conclusion=None)], "failing"),
])
def test_ci_state(runs, state):
    assert report.ci_state(runs) == state


@pytest.mark.parametrize("pr,expected", [
    ({"author": {"login": "app/dependabot"}, "headRefName": "dependabot/pip/backend/x-1"}, True),
    ({"author": {"login": "dependabot[bot]"}, "headRefName": "whatever"}, True),
    ({"author": {"login": "bradycai"}, "headRefName": "dependabot/npm_and_yarn/frontend/x"}, True),
    ({"author": {"login": "bradycai"}, "headRefName": "agent/39-dev-automation"}, False),
    ({}, False),
])
def test_is_dependabot(pr, expected):
    assert report.is_dependabot(pr) is expected


def test_age_days():
    assert report.age_days("2026-11-10T12:00:00Z", NOW) == 7
    assert report.age_days("2026-11-17T11:00:00Z", NOW) == 0
    assert report.age_days("not a date", NOW) == -1


def test_cells_neutralize_untrusted_titles():
    cell = report._cell("evil | title <script>alert(1)</script>\n`x`")
    assert "|" not in cell.replace("\\|", "") and "<" not in cell and "\n" not in cell and "`" not in cell
    assert len(report._cell("x" * 500)) == 90


# --------------------------------------------------------------------------- #
# render_report                                                               #
# --------------------------------------------------------------------------- #
def _pr(number, title, branch, *, draft=False, login="bradycai", labels=(), created="2026-11-03T12:00:00Z",
        body=""):
    return {"number": number, "title": title, "headRefName": branch, "isDraft": draft,
            "author": {"login": login}, "labels": [{"name": n} for n in labels], "createdAt": created,
            "body": body}


def test_render_report_sections_and_triage():
    prs = [
        _pr(57, "Dev automation", "agent/39-dev-automation", draft=True),
        _pr(58, "Bump anthropic from 0.40.0 to 0.41.0 in /backend", "dependabot/pip/backend/anthropic-0.41.0",
            login="app/dependabot"),
        _pr(59, "Bump httpx from 0.27.0 to 0.27.2 in /backend", "dependabot/pip/backend/httpx-0.27.2",
            login="app/dependabot"),
        _pr(60, "Bump requests from 2.31.0 to 2.32.0 | <b>x</b>", "dependabot/pip/backend/requests-2.32.0",
            login="app/dependabot", labels=["security"]),
    ]
    ci = {57: "failing", 58: "passing", 59: "passing", 60: "pending"}
    failed = [{"name": "CI", "event": "push", "conclusion": "failure", "created_at": "2026-11-15T09:00:00Z",
               "html_url": "https://github.com/o/r/actions/runs/1"}]
    md = report.render_report("o/r", "Production", prs, ci, failed, 7, NOW)

    assert md.startswith("# Maintenance triage: o/r")
    assert "Open pull requests: **4** (1 draft, 3 ready); failing CI: **1**" in md
    assert "Failed runs on `Production` in the last 7 days: **1**" in md
    assert "security **1**, high-risk **1**, major **0**, minor-patch **1**" in md
    needs = md.split("## Needs a human")[1].split("## Open pull requests")[0]
    assert "#60 (security)" in needs and "#58 (high-risk)" in needs and "#59" not in needs
    assert "PR #57 has failing CI" in needs and "https://github.com/o/r/actions/runs/1" in needs
    assert "| #57 | Dev automation | draft | failing | 14 | agent/39-dev-automation |" in md
    deps = md.split("## Dependabot pull requests")[1]
    assert deps.index("#60") < deps.index("#58") < deps.index("#59")  # most urgent first
    assert "<b>" not in md and "\\|" in md


def test_render_report_empty_and_incomplete():
    errors = ["open pull requests: HTTP 403"]
    md = report.render_report("o/r", "Production", [], {}, [], 7, NOW, errors=errors)
    assert "Incomplete report" in md and "HTTP 403" in md
    assert "Nothing needs attention." in md and "No open pull requests." in md
    assert "No failed runs." in md and "No open Dependabot pull requests." in md


# --------------------------------------------------------------------------- #
# The workflow stays read-only                                                #
# --------------------------------------------------------------------------- #
def test_maintenance_workflow_is_read_only():
    yaml = pytest.importorskip("yaml")
    path = _PATH.parents[1] / ".github" / "workflows" / "maintenance.yml"
    text = path.read_text()
    wf = yaml.safe_load(text)
    on = wf.get("on", wf.get(True))
    assert set(on) == {"schedule", "workflow_dispatch"}
    assert wf["permissions"] == {"contents": "read", "pull-requests": "read", "actions": "read"}
    assert "secrets." not in text.replace("repository secrets.", "") and "anthropics/" not in text
    assert "${{ secrets" not in text and "GITHUB_STEP_SUMMARY" in text
    for job in wf["jobs"].values():
        assert "permissions" not in job
