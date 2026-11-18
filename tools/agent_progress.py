#!/usr/bin/env python3
"""Progress board for the parallel agent workflow (see CLAUDE.md, "Parallel agent workflow").

Each agent reports its issue's progress into `.progress/issue-<n>.json`; the lead renders all
of them into `PROGRESS.txt` at the repository root, a plain-text board with a progress bar per
issue and the number of agents currently working. Both are local state (gitignored): the board
rewrites itself every minute, so it is never committed.

Commands (run from anywhere; paths resolve to the repository root):
  seed <issue> --title T --branch B --agent A   register an issue as queued
  update <issue> <percent> <state> "<step>"     agents report: state = working | done | blocked
  integrate <issue> "<note>"                    lead: reviewed, verified, pushed (100%)
  finished "<line>"                             lead: add a line to the "Already finished" list
  render                                        write PROGRESS.txt once
  watch [--interval 60]                         re-render every interval until every issue is
                                                done/integrated/blocked (then render once more)
  reset                                         clear the current issue set (keeps "finished")

Agents call it with an absolute path, because they run in separate git worktrees:
  python3 "/path/to/repo/tools/agent_progress.py" update 12 40 working "migration written"
"""
import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, ".progress")
BOARD = os.path.join(ROOT, "PROGRESS.txt")
FINISHED = os.path.join(STATE_DIR, "finished.json")
STATES = ("queued", "working", "done", "blocked", "integrated")
FINAL_STATES = ("done", "blocked", "integrated")
STALE_AFTER_SEC = 900


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_json(path: str, data) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)  # atomic: the board never reads a half-written file


def _issue_path(issue: int) -> str:
    return os.path.join(STATE_DIR, f"issue-{issue}.json")


def _load(issue: int) -> dict:
    path = _issue_path(issue)
    return json.load(open(path)) if os.path.exists(path) else {"issue": issue}


def _save(issue: int, **fields) -> dict:
    data = _load(issue)
    data.update(fields, updated_at=_now().isoformat())
    _write_json(_issue_path(issue), data)
    return data


def _issues() -> list[dict]:
    items = []
    for path in glob.glob(os.path.join(STATE_DIR, "issue-*.json")):
        try:
            items.append(json.load(open(path)))
        except (OSError, json.JSONDecodeError):
            pass  # a file mid-write is picked up on the next render
    return sorted(items, key=lambda i: i.get("issue", 0))


def _bar(pct: int, width: int = 30) -> str:
    filled = round(width * max(0, min(100, pct)) / 100)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def _ago(ts: str) -> str:
    try:
        secs = (_now() - datetime.fromisoformat(ts)).total_seconds()
    except (TypeError, ValueError):
        return "?"
    return f"{int(secs)}s ago" if secs < 90 else f"{int(secs // 60)}m ago"


def render() -> bool:
    """Write PROGRESS.txt. Returns True when every issue is in a final state."""
    items = _issues()
    working = [i for i in items if i.get("state") == "working"]
    overall = round(sum(i.get("percent", 0) for i in items) / len(items)) if items else 0
    finished = json.load(open(FINISHED)) if os.path.exists(FINISHED) else []
    lines = [
        "AI TRADING ASSISTANT — AGENTIC WORKFLOW PROGRESS",
        f"Updated {_now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}  (refreshes every minute)",
        "",
        f"Agents currently working: {len(working)} of {len(items)}",
        f"Overall progress:         {_bar(overall)} {overall:3d}%",
        "",
    ]
    for i in items:
        state = i.get("state", "queued")
        stale = ""
        if state == "working":
            try:
                if (_now() - datetime.fromisoformat(i["updated_at"])).total_seconds() > STALE_AFTER_SEC:
                    stale = "  (no update for 15m+)"
            except (KeyError, ValueError):
                pass
        lines += [
            f"#{i['issue']:<3} {i.get('title', '')}",
            f"     {_bar(i.get('percent', 0))} {i.get('percent', 0):3d}%  {state.upper():<10} "
            f"agent: {i.get('agent', '-')}  updated {_ago(i.get('updated_at', ''))}{stale}",
            f"     step: {i.get('step', '-')}",
            f"     branch: {i.get('branch', '-')}",
            "",
        ]
    if finished:
        lines.append("Already finished:")
        lines += [f"  {line}" for line in finished]
        lines.append("")
    lines += [
        "Legend: QUEUED = not started; WORKING = an agent is on it; DONE = agent finished, awaiting",
        "        lead review; INTEGRATED = reviewed, verified and pushed; BLOCKED = needs a decision.",
    ]
    tmp = BOARD + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, BOARD)
    return bool(items) and all(i.get("state") in FINAL_STATES for i in items)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("issue", type=int)
    s.add_argument("--title", required=True)
    s.add_argument("--branch", default="-")
    s.add_argument("--agent", default="-")
    u = sub.add_parser("update")
    u.add_argument("issue", type=int)
    u.add_argument("percent", type=int)
    u.add_argument("state", choices=STATES)
    u.add_argument("step", nargs="+")
    g = sub.add_parser("integrate")
    g.add_argument("issue", type=int)
    g.add_argument("note", nargs="+")
    f = sub.add_parser("finished")
    f.add_argument("line", nargs="+")
    sub.add_parser("render")
    w = sub.add_parser("watch")
    w.add_argument("--interval", type=int, default=60)
    w.add_argument("--max-hours", type=float, default=8.0)
    sub.add_parser("reset")
    a = ap.parse_args(argv)

    if a.cmd == "seed":
        _save(a.issue, title=a.title, branch=a.branch, agent=a.agent, percent=0, state="queued",
              step="waiting to start")
    elif a.cmd == "update":
        d = _save(a.issue, percent=max(0, min(100, a.percent)), state=a.state, step=" ".join(a.step)[:160])
        print(f"issue {a.issue}: {d['percent']}% {d['state']} — {d['step']}")
    elif a.cmd == "integrate":
        _save(a.issue, percent=100, state="integrated", step=" ".join(a.note)[:160])
    elif a.cmd == "finished":
        finished = json.load(open(FINISHED)) if os.path.exists(FINISHED) else []
        finished.append(" ".join(a.line))
        _write_json(FINISHED, finished)
    elif a.cmd == "reset":
        for path in glob.glob(os.path.join(STATE_DIR, "issue-*.json")):
            os.remove(path)
    elif a.cmd == "watch":
        deadline = time.monotonic() + a.max_hours * 3600
        while not render() and time.monotonic() < deadline:
            time.sleep(a.interval)
        render()
        return 0
    if a.cmd != "watch":
        render()
    return 0


if __name__ == "__main__":
    sys.exit(main())
