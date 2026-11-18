#!/usr/bin/env python3
"""Weekly maintenance triage: open PRs, failed default-branch runs, and Dependabot PRs by risk.

Read-only. It needs only the GitHub CLI (`gh`) authenticated with a token that can read pull
requests and Actions runs; in .github/workflows/maintenance.yml that is the default GITHUB_TOKEN
with `contents: read, pull-requests: read, actions: read`. No Claude, no secrets, no writes.

The report is Markdown, written to $GITHUB_STEP_SUMMARY when set (and always to stdout):
  1. open pull requests: draft or ready, CI state on the head commit, age;
  2. failed workflow runs on the default branch in the last --days days;
  3. Dependabot pull requests classified per the roadmap (see classify_dependabot):
       security    : a security fix (security label, or "security" in the title or branch)
       high-risk   : anthropic, alpaca-py, SQLAlchemy, psycopg*, alembic, or GitHub Actions
       major       : a major version bump of any other package (also high-risk: needs a human)
       minor-patch : a routine minor or patch update
     Anything that can't be read safely is classified up (fail closed), never down.

Usage:
  python3 tools/maintenance_report.py [--repo owner/name] [--days 7] [--output report.md]
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

CATEGORIES = ("security", "high-risk", "major", "minor-patch")  # most to least urgent
NEEDS_HUMAN = ("security", "high-risk", "major")
HIGH_RISK_PACKAGES = re.compile(
    r"(?<![a-z0-9])(anthropic|alpaca[a-z0-9_.-]*|sqlalchemy|psycopg[a-z0-9_.\[\]-]*|alembic)(?![a-z0-9])")
FAILED_RUN_CONCLUSIONS = ("failure", "timed_out", "startup_failure")
OK_CONCLUSIONS = {"success", "skipped", "neutral"}


# --------------------------------------------------------------------------- #
# Pure classification                                                         #
# --------------------------------------------------------------------------- #
_VERSION_PART = re.compile(r"[<>=~!^v\s]*(\d+(?:\.\d+)*)(?:[.+-]?[A-Za-z][0-9A-Za-z.+-]*)?")


def _versions(text: str) -> tuple[int, ...] | None:
    """The highest version in a version or requirement string ('<1.15', '>=2.0,<3', 'v4').
    None when any part isn't a plain version (a commit SHA, a URL): the caller fails closed."""
    found = []
    for part in (text or "").split(","):
        part = part.strip()
        if re.fullmatch(r"[0-9a-f]{7,40}", part) and re.search(r"[a-f]", part):
            return None  # a commit SHA, not a version
        m = _VERSION_PART.fullmatch(part)
        if not m:
            return None
        found.append(tuple(int(p) for p in m.group(1).split(".")))
    return max(found) if found else None


def version_change(title: str) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    """(old, new) from 'Bump x from A to B' / 'Update x requirement from A to B', else None."""
    m = re.search(r"\bfrom\s+(\S+(?:\s*,\s*\S+)*)\s+to\s+(\S+(?:\s*,\s*\S+)*)", title or "", re.I)
    if not m:
        return None
    old, new = _versions(m.group(1)), _versions(m.group(2))
    return (old, new) if old and new else None


def is_major_bump(old: tuple[int, ...], new: tuple[int, ...]) -> bool:
    """Semver: the first component changed, or for 0.x the second one did (0.y may break)."""
    old_major, new_major = old[0], new[0]
    if old_major != new_major:
        return True
    if old_major == 0:
        return (old[1] if len(old) > 1 else 0) != (new[1] if len(new) > 1 else 0)
    return False


def _is_github_actions(title: str, branch: str, labels: list[str]) -> bool:
    # An action is `owner/name` (npm scoped packages start with @, pip and npm names have no /).
    return ("/github_actions/" in f"/{branch}/" or "github_actions" in labels or "github-actions" in labels
            or bool(re.search(r"\bbump\s+(?!@)[\w.-]+/[\w./-]+\s+from\b", title, re.I)))


def classify_dependabot(title: str, branch: str = "", labels=(), summary: str = "") -> tuple[str, str]:
    """Classify a Dependabot PR as (category, reason). Pure: no I/O.

    `summary` is optional extra text naming the packages (the first paragraph of the PR body,
    which lists a group's packages); release notes must not be passed, as they mention other
    libraries. Precedence: security > high-risk > major > minor-patch.
    """
    title, branch = title or "", branch or ""
    labels = [str(lbl).lower() for lbl in labels or ()]
    text = f"{title}\n{branch}\n{summary or ''}".lower()
    reasons: list[str] = []
    category = "minor-patch"

    if (any("security" in lbl for lbl in labels) or re.search(r"\bsecurity\b", title.lower())
            or "security" in branch.lower()):
        category = "security"
        reasons.append("security fix")

    risky = sorted({m.group(1) for m in HIGH_RISK_PACKAGES.finditer(text)})
    if risky:
        reasons.append(f"high-risk package: {', '.join(risky)}")
    if _is_github_actions(title, branch, labels):
        reasons.append("GitHub Actions update")
    if (risky or _is_github_actions(title, branch, labels)) and category == "minor-patch":
        category = "high-risk"

    group = re.search(r"\bbump the (\S+) group\b", title, re.I)
    change = version_change(title)
    if change:
        old, new = change
        old_s, new_s = ".".join(map(str, old)), ".".join(map(str, new))
        if is_major_bump(old, new):
            reasons.append(f"major version {old_s} -> {new_s}")
            if category == "minor-patch":
                category = "major"
        else:
            reasons.append(f"minor/patch {old_s} -> {new_s}")
    elif group:
        name = group.group(1).lower()
        if "minor" in name or "patch" in name:
            reasons.append(f"group {name} (minor and patch updates only)")
        else:
            reasons.append(f"group {name}: update types unknown")
            if category == "minor-patch":
                category = "major"
    else:
        reasons.append("could not read the version change")
        if category == "minor-patch":
            category = "major"
    return category, "; ".join(reasons)


def is_dependabot(pr: dict) -> bool:
    login = ((pr.get("author") or {}).get("login") or "").lower()
    return "dependabot" in login or (pr.get("headRefName") or "").startswith("dependabot/")


def ci_state(runs: list[dict]) -> str:
    """Summarize Actions runs on one commit: latest run per workflow decides."""
    latest: dict = {}
    for r in runs:
        key = r.get("workflow_id") or r.get("name")
        if key not in latest or (r.get("run_number") or 0) > (latest[key].get("run_number") or 0):
            latest[key] = r
    if not latest:
        return "no runs"
    states = []
    for r in latest.values():
        if r.get("status") != "completed":
            states.append("pending")
        elif r.get("conclusion") in OK_CONCLUSIONS:
            states.append("passing")
        else:
            states.append("failing")
    for s in ("failing", "pending"):
        if s in states:
            return s
    return "passing"


def age_days(created_at: str, now: datetime) -> int:
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return -1
    return max(0, (now - created).days)


def _cell(text, limit: int = 90) -> str:
    """Untrusted text (PR titles) made safe for one Markdown table cell."""
    s = " ".join(str(text or "").split())
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return (s.replace("\\", "\\\\").replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")
            .replace("`", "'"))


def _first_paragraph(body: str) -> str:
    return (body or "").strip().split("\n\n", 1)[0]


def render_report(repo: str, default_branch: str, prs: list[dict], ci: dict, failed_runs: list[dict],
                  days: int, now: datetime, errors: list[str] = ()) -> str:
    """Build the Markdown report. Pure: every input is already fetched."""
    deps = []
    for pr in prs:
        if is_dependabot(pr):
            cat, why = classify_dependabot(pr.get("title", ""), pr.get("headRefName", ""),
                                           [lbl.get("name", "") for lbl in pr.get("labels") or []],
                                           _first_paragraph(pr.get("body", "")))
            deps.append((CATEGORIES.index(cat), cat, why, pr))
    deps.sort(key=lambda d: (d[0], d[3].get("number", 0)))
    counts = {c: sum(1 for d in deps if d[1] == c) for c in CATEGORIES}
    failing = [pr for pr in prs if ci.get(pr.get("number")) == "failing"]
    drafts = sum(1 for pr in prs if pr.get("isDraft"))

    out = [
        f"# Maintenance triage: {repo}",
        "",
        f"Generated {now.strftime('%Y-%m-%d %H:%M UTC')}. Read-only report; nothing was changed.",
        "",
        f"- Open pull requests: **{len(prs)}** ({drafts} draft, {len(prs) - drafts} ready); "
        f"failing CI: **{len(failing)}**",
        f"- Failed runs on `{default_branch}` in the last {days} days: **{len(failed_runs)}**",
        "- Dependabot: " + ", ".join(f"{c} **{counts[c]}**" for c in CATEGORIES),
    ]
    attention = [d for d in deps if d[1] in NEEDS_HUMAN]
    if errors:
        out += ["", "> **Incomplete report.** These sections could not be loaded:"]
        out += [f"> - {_cell(e, 200)}" for e in errors]

    out += ["", "## Needs a human", ""]
    items = [f"- Dependabot #{d[3]['number']} ({d[1]}): {_cell(d[3].get('title'))} ({_cell(d[2], 120)})"
             for d in attention]
    items += [f"- PR #{pr['number']} has failing CI: {_cell(pr.get('title'))}" for pr in failing]
    items += [f"- `{_cell(r.get('name'))}` failed on `{default_branch}` "
              f"({_cell(r.get('created_at', '')[:10])}): {r.get('html_url', '')}" for r in failed_runs[:10]]
    out += items or ["Nothing needs attention."]

    out += ["", "## Open pull requests", ""]
    if prs:
        out += ["| PR | Title | Status | CI | Age (days) | Branch |", "|---|---|---|---|---|---|"]
        for pr in sorted(prs, key=lambda p: p.get("number", 0)):
            status = "draft" if pr.get("isDraft") else "ready"
            out.append(f"| #{pr.get('number')} | {_cell(pr.get('title'))} | {status} | "
                       f"{ci.get(pr.get('number'), 'unknown')} | {age_days(pr.get('createdAt', ''), now)} | "
                       f"{_cell(pr.get('headRefName'), 50)} |")
    else:
        out.append("No open pull requests.")

    out += ["", f"## Failed runs on `{default_branch}` (last {days} days)", ""]
    if failed_runs:
        out += ["| Workflow | Event | Conclusion | Started | Run |", "|---|---|---|---|---|"]
        for r in failed_runs:
            out.append(f"| {_cell(r.get('name'))} | {_cell(r.get('event'))} | {_cell(r.get('conclusion'))} | "
                       f"{_cell(r.get('created_at', '')[:16])} | {r.get('html_url', '')} |")
    else:
        out.append("No failed runs.")

    out += ["", "## Dependabot pull requests", ""]
    if deps:
        out += ["| PR | Category | Why | CI | Age (days) |", "|---|---|---|---|---|"]
        for _, cat, why, pr in deps:
            out.append(f"| #{pr.get('number')} {_cell(pr.get('title'), 70)} | **{cat}** | "
                       f"{_cell(why, 120)} | {ci.get(pr.get('number'), 'unknown')} | "
                       f"{age_days(pr.get('createdAt', ''), now)} |")
        out += ["", "Only `minor-patch` updates are routine. Security, high-risk and major updates "
                "always need a human review (CODEOWNERS); a passing build alone never authorizes a "
                "change to execution or risk behavior."]
    else:
        out.append("No open Dependabot pull requests.")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# GitHub I/O (gh CLI, read-only)                                              #
# --------------------------------------------------------------------------- #
def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def _gh_json(*args: str):
    return json.loads(_gh(*args) or "null")


def collect(repo: str, days: int, now: datetime) -> tuple[str, list, dict, list, list[str]]:
    errors: list[str] = []

    def attempt(what, fn, default):
        try:
            return fn()
        except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError) as e:
            detail = getattr(e, "stderr", "") or str(e)
            errors.append(f"{what}: {detail.strip()[:200]}")
            return default

    default_branch = attempt("default branch", lambda: _gh(
        "api", f"repos/{repo}", "--jq", ".default_branch").strip() or "Production", "Production")
    prs = attempt("open pull requests", lambda: _gh_json(
        "pr", "list", "--repo", repo, "--state", "open", "--limit", "100", "--json",
        "number,title,isDraft,createdAt,author,headRefName,headRefOid,labels,body"), [])
    ci: dict = {}
    for pr in prs:
        ci[pr["number"]] = attempt(f"CI for #{pr['number']}", lambda pr=pr: ci_state(_gh_json(
            "api", "-X", "GET", f"repos/{repo}/actions/runs", "-f", f"head_sha={pr['headRefOid']}",
            "-f", "per_page=100", "--jq", ".workflow_runs")), "unknown")
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    failed: list[dict] = []
    # Cancelled runs are left out: CI cancels superseded runs (concurrency), which isn't a failure.
    for conclusion in FAILED_RUN_CONCLUSIONS:
        failed += attempt(f"{conclusion} runs on {default_branch}", lambda c=conclusion: _gh_json(
            "api", "-X", "GET", f"repos/{repo}/actions/runs", "-f", f"branch={default_branch}",
            "-f", f"status={c}", "-f", f"created=>={since}", "-f", "per_page=100",
            "--jq", ".workflow_runs"), []) or []
    failed.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return default_branch, prs, ci, failed, errors


def _current_repo() -> str:
    env = os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY")
    if env:
        return env
    return _gh_json("repo", "view", "--json", "nameWithOwner")["nameWithOwner"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", help="owner/name (default: GH_REPO, GITHUB_REPOSITORY or the current repo)")
    ap.add_argument("--days", type=int, default=7, help="failed-run window on the default branch")
    ap.add_argument("--output", help="also write the report here (default: $GITHUB_STEP_SUMMARY if set)")
    a = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    repo = a.repo or _current_repo()
    default_branch, prs, ci, failed, errors = collect(repo, a.days, now)
    report = render_report(repo, default_branch, prs, ci, failed, a.days, now, errors)
    print(report)
    target = a.output or os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a") as f:
            f.write(report)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
