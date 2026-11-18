#!/usr/bin/env python3
"""Auto-merge policy: merge a pull request automatically ONLY when everything is good.

A PR is merged when ALL of these hold (any failure = left for a human):
  1. auto-merge is switched on (repository variable AUTOMERGE_ENABLED=true; checked by the
     workflow, and by --require-enabled locally);
  2. the PR carries the `automerge` label (explicit opt-in) and is open, not a draft, and
     from a branch of this repository (never a fork);
  3. it targets the default branch (stacked PRs must be retargeted first, so nothing is
     merged into an intermediate branch that has already moved on);
  4. every required CI job passed on the PR's CURRENT head commit and no check failed or is
     still running;
  5. no review currently requests changes, and GitHub reports it mergeable without conflicts;
  6. it changes NO file covered by .github/CODEOWNERS — risk, execution, broker, automation,
     monitor, config, models, migrations, secrets templates, CI and agent rules always need
     explicit human review, as the roadmap requires.
The merge is pinned to the evaluated head commit (`--match-head-commit`), so a push that lands
during evaluation cancels it.

Usage:
  python3 tools/automerge_policy.py --pr 16            # dry run: print the decision
  python3 tools/automerge_policy.py --pr 16 --merge    # merge if eligible
  python3 tools/automerge_policy.py --all --merge      # every open PR labeled `automerge`
Requires the GitHub CLI (`gh`) authenticated for the repository (GH_TOKEN in Actions).
"""
import argparse
import fnmatch
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODEOWNERS = os.path.join(ROOT, ".github", "CODEOWNERS")
LABEL = "automerge"
REQUIRED_CHECKS = ("Backend (lint + tests)", "Frontend (install + build)")
OK_CONCLUSIONS = {"SUCCESS", "SKIPPED", "NEUTRAL"}
# Always protected, even if CODEOWNERS is edited or missing: the policy and its workflow.
ALWAYS_PROTECTED = ("tools/automerge_policy.py", ".github/")


def load_protected_patterns(path: str = CODEOWNERS) -> list[str]:
    patterns = list(ALWAYS_PROTECTED)
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line.split()[0].lstrip("/"))
    except FileNotFoundError:
        pass  # fall back to ALWAYS_PROTECTED; the CODEOWNERS file itself lives under .github/
    return patterns


def is_protected(path: str, patterns: list[str]) -> bool:
    path = path.lstrip("/")
    for p in patterns:
        if p.endswith("/"):
            if path.startswith(p):
                return True
        elif path == p or fnmatch.fnmatch(path, p):
            return True
    return False


def evaluate(pr: dict, files: list[str], patterns: list[str], default_branch: str) -> tuple[bool, list[str]]:
    """Pure decision. `pr` is `gh pr view --json` output (see FIELDS). Returns (ok, reasons)."""
    reasons: list[str] = []
    labels = {lbl.get("name") for lbl in pr.get("labels") or []}
    if LABEL not in labels:
        reasons.append(f"missing the `{LABEL}` label")
    if pr.get("state") != "OPEN":
        reasons.append(f"state is {pr.get('state')}")
    if pr.get("isDraft"):
        reasons.append("still a draft")
    if pr.get("isCrossRepository"):
        reasons.append("from a fork")
    if pr.get("baseRefName") != default_branch:
        reasons.append(f"targets {pr.get('baseRefName')}, not {default_branch} (retarget stacked PRs first)")
    if pr.get("reviewDecision") == "CHANGES_REQUESTED":
        reasons.append("a review requests changes")
    if pr.get("mergeable") != "MERGEABLE":
        reasons.append(f"not mergeable ({pr.get('mergeable')})")
    if pr.get("mergeStateStatus") in ("DIRTY", "BLOCKED", "UNKNOWN"):
        reasons.append(f"merge state {pr.get('mergeStateStatus')}")

    checks = {}
    for c in pr.get("statusCheckRollup") or []:
        name = c.get("name") or c.get("context")
        state = (c.get("conclusion") or c.get("state") or "").upper()
        status = (c.get("status") or "COMPLETED").upper()
        checks[name] = (status, state)
        if status != "COMPLETED":
            reasons.append(f"check `{name}` is {status.lower()}")
        elif state not in OK_CONCLUSIONS:
            reasons.append(f"check `{name}` concluded {state.lower() or 'unknown'}")
    for name in REQUIRED_CHECKS:
        if checks.get(name, (None, None))[1] != "SUCCESS":
            reasons.append(f"required check `{name}` has not passed")

    protected = [f for f in files if is_protected(f, patterns)]
    if protected:
        shown = ", ".join(protected[:5]) + (" …" if len(protected) > 5 else "")
        reasons.append(f"changes CODEOWNERS-protected files (human review required): {shown}")
    return not reasons, reasons


# --------------------------------------------------------------------------- #
# GitHub I/O (gh CLI)                                                         #
# --------------------------------------------------------------------------- #
FIELDS = ("number,title,state,isDraft,isCrossRepository,baseRefName,headRefOid,labels,"
          "reviewDecision,mergeable,mergeStateStatus,statusCheckRollup,files")


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def _default_branch() -> str:
    return json.loads(_gh("repo", "view", "--json", "defaultBranchRef"))["defaultBranchRef"]["name"]


def _pr(number: int) -> dict:
    return json.loads(_gh("pr", "view", str(number), "--json", FIELDS))


def _labeled_open_prs() -> list[int]:
    out = json.loads(_gh("pr", "list", "--state", "open", "--label", LABEL, "--json", "number"))
    return [p["number"] for p in out]


def process(number: int, *, merge: bool, patterns: list[str], default_branch: str) -> bool:
    pr = _pr(number)
    files = [f["path"] for f in pr.get("files") or []]
    ok, reasons = evaluate(pr, files, patterns, default_branch)
    head = pr.get("headRefOid", "")[:7]
    if not ok:
        print(f"#{number} @{head} NOT merged:")
        for r in reasons:
            print(f"  - {r}")
        return False
    if not merge:
        print(f"#{number} @{head} eligible (dry run; pass --merge to merge)")
        return True
    _gh("pr", "merge", str(number), "--merge", "--match-head-commit", pr["headRefOid"])
    print(f"#{number} @{head} MERGED into {default_branch}")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--pr", type=int, help="evaluate one pull request")
    target.add_argument("--all", action="store_true", help=f"every open PR labeled `{LABEL}`")
    ap.add_argument("--merge", action="store_true", help="merge eligible PRs (default: dry run)")
    ap.add_argument("--require-enabled", action="store_true",
                    help="do nothing unless AUTOMERGE_ENABLED=true in the environment")
    a = ap.parse_args(argv)
    if a.require_enabled and os.environ.get("AUTOMERGE_ENABLED", "").lower() != "true":
        print("auto-merge is switched off (AUTOMERGE_ENABLED is not 'true'); nothing done")
        return 0
    patterns = load_protected_patterns()
    default_branch = _default_branch()
    numbers = [a.pr] if a.pr else _labeled_open_prs()
    if not numbers:
        print(f"no open PRs labeled `{LABEL}`")
    for n in numbers:
        try:
            process(n, merge=a.merge, patterns=patterns, default_branch=default_branch)
        except subprocess.CalledProcessError as e:
            print(f"#{n} error: {(e.stderr or e.stdout or str(e)).strip()[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
