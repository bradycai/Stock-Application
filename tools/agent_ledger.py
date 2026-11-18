#!/usr/bin/env python3
"""Agent task ledger: who is working on which issue, under which limits (see CLAUDE.md, "Parallel
agent workflow", and .claude/skills/run-agent-workflow).

The ledger stops the agent workflow from doing the same work twice or running away. Each issue has
at most one ACTIVE claim recording: issue, triggering actor and event, owner agent, branch, PR,
state, retries, budget (USD estimate) and spend, latest commit, and the file globs it owns.
It REFUSES (exit code 1, nothing written) to:
  - claim an issue that already has an active claim (dedup);
  - claim files whose globs overlap another active claim's globs;
  - claim more than --max-per-hour issues in any rolling hour;
  - attempt more than --max-retries repair loops, or once the spend estimate reaches the budget;
  - claim or attempt anything while the global pause switch is on.
While paused, `update`, `release` and `status` still work, so in-flight work can be recorded and
wound down.

State is local and gitignored, like tools/agent_progress.py: `.progress/ledger.json` at the
repository root. Writes go through a per-process temp file and an atomic `os.replace`, under an
exclusive lock (`fcntl`, where available) so two concurrent claims can't both succeed. A ledger
that can't be read is never overwritten: every command fails closed until it is fixed.

Commands (run from anywhere; the default ledger path resolves to the repository root):
  claim <issue> --agent A --branch B --files "glob,glob" [--actor X] [--event E] [--budget USD]
                [--max-per-hour N]
  attempt <issue> [--max-retries 3]                     one more repair loop on an active claim
  update <issue> [--state S] [--pr N] [--commit SHA] [--branch B] [--agent A] [--budget USD]
                 [--spent USD] [--note TEXT]
  release <issue> [--outcome integrated|abandoned|failed] [--note TEXT]
  pause [--reason TEXT] | resume                        the global pause switch
  status [<issue>] [--all] [--json]
Every command accepts --ledger PATH (before the command) to use another ledger file.

File globs are paths relative to the repository root: `*` and `?` stay within one path segment,
`**` spans directories, and a pattern also owns everything below it (`frontend/src` owns
`frontend/src/App.jsx`) unless it ends in a plain file name. Overlap is decided conservatively:
when in doubt, two globs overlap.

Agents in worktrees call it with an absolute path, like agent_progress.py:
  python3 "/path/to/repo/tools/agent_ledger.py" update 39 --commit "$(git rev-parse HEAD)"
"""
import argparse
import contextlib
import fnmatch
import functools
import json
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: no advisory lock, atomic writes still apply
    fcntl = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LEDGER = os.path.join(ROOT, ".progress", "ledger.json")
ACTIVE_STATES = ("claimed", "working", "review", "blocked")
RELEASED = "released"
OUTCOMES = ("integrated", "abandoned", "failed")
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_PER_HOUR = 6
HISTORY_LIMIT = 200
WINDOW = timedelta(hours=1)


class Refused(Exception):
    """The ledger refuses the request; nothing was written."""


class LedgerError(Exception):
    """The ledger file can't be read; fail closed."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def empty_state() -> dict:
    return {"paused": False, "paused_reason": "", "paused_at": None, "claims": {}, "claim_log": [],
            "history": []}


# --------------------------------------------------------------------------- #
# File globs                                                                  #
# --------------------------------------------------------------------------- #
_WILD = "*?["


def parse_globs(text: str | None) -> list[str]:
    """Split "a,b" into normalized globs: no leading ./ or /, no empty or duplicate entries."""
    out: list[str] = []
    for raw in (text or "").split(","):
        g = raw.strip()
        while g.startswith("./"):
            g = g[2:]
        g = g.lstrip("/")
        if g.endswith("/"):
            g += "**"
        if g and g not in out:
            out.append(g)
    return out


def _literal_prefix(seg: str) -> str:
    cut = min((seg.index(c) for c in _WILD if c in seg), default=len(seg))
    return seg[:cut]


def _literal_suffix(seg: str) -> str:
    cut = max((seg.rindex(c) for c in "*?]" if c in seg), default=-1)
    return seg[cut + 1:]


def _segment_overlap(a: str, b: str) -> bool:
    """Can one path segment match both patterns? Exact for literals, conservative otherwise."""
    a_wild, b_wild = any(c in a for c in _WILD), any(c in b for c in _WILD)
    if not a_wild and not b_wild:
        return a == b
    if not a_wild:
        return fnmatch.fnmatchcase(a, b)
    if not b_wild:
        return fnmatch.fnmatchcase(b, a)
    pa, pb = _literal_prefix(a), _literal_prefix(b)
    sa, sb = _literal_suffix(a), _literal_suffix(b)
    return (pa.startswith(pb) or pb.startswith(pa)) and (sa.endswith(sb) or sb.endswith(sa))


@functools.lru_cache(maxsize=4096)
def _segments_overlap(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    if not a or not b:
        rest = a or b
        return all(s == "**" for s in rest)
    if a[0] == "**":
        return _segments_overlap(a[1:], b) or _segments_overlap(a, b[1:])
    if b[0] == "**":
        return _segments_overlap(a, b[1:]) or _segments_overlap(a[1:], b)
    return _segment_overlap(a[0], b[0]) and _segments_overlap(a[1:], b[1:])


def _owned_segments(glob: str) -> tuple[str, ...]:
    """Path segments a glob owns. A glob also owns everything below it, unless its last part is a
    plain file name (`App.jsx`, `CLAUDE.md`; but `.github` and `src` are treated as directories)."""
    segs = tuple(s for s in glob.split("/") if s)
    last = segs[-1] if segs else ""
    is_file = bool(last) and not any(c in last for c in _WILD) and "." in last.lstrip(".")
    return segs if is_file else segs + ("**",)


def globs_overlap(a: str, b: str) -> bool:
    """True when some path could be owned by both globs."""
    return _segments_overlap(_owned_segments(a), _owned_segments(b))


def overlapping(globs: list[str], others: list[str]) -> list[tuple[str, str]]:
    return [(g, o) for g in globs for o in others if globs_overlap(g, o)]


# --------------------------------------------------------------------------- #
# Pure operations on the state dict (raise Refused; the caller saves)         #
# --------------------------------------------------------------------------- #
def _active(state: dict, issue: int) -> dict | None:
    rec = state["claims"].get(str(issue))
    return rec if rec and rec.get("state") in ACTIVE_STATES else None


def _require_active(state: dict, issue: int) -> dict:
    rec = _active(state, issue)
    if rec is None:
        raise Refused(f"issue {issue} has no active claim")
    return rec


def _refuse_if_paused(state: dict, what: str) -> None:
    if state.get("paused"):
        reason = state.get("paused_reason") or "no reason given"
        raise Refused(f"the ledger is paused ({reason}); no {what} until `resume`")


def _recent_claims(state: dict, now: datetime) -> list[str]:
    recent = []
    for ts in state.get("claim_log", []):
        try:
            if now - datetime.fromisoformat(ts) < WINDOW:
                recent.append(ts)
        except (TypeError, ValueError):
            recent.append(now.isoformat())  # unreadable entry: count it (fail closed)
    return recent


def claim(state: dict, issue: int, *, agent: str, branch: str, files: list[str], actor: str = "lead",
          event: str = "manual", budget_usd: float = 0.0, max_per_hour: int = DEFAULT_MAX_PER_HOUR,
          now: datetime | None = None) -> dict:
    now = now or _now()
    if issue <= 0:
        raise Refused(f"invalid issue number {issue}")
    if budget_usd < 0:
        raise Refused("budget must not be negative")
    _refuse_if_paused(state, "new claims")
    current = _active(state, issue)
    if current is not None:
        raise Refused(f"issue {issue} is already claimed by {current.get('agent')} "
                      f"on {current.get('branch')} (state {current.get('state')})")
    recent = _recent_claims(state, now)
    if len(recent) >= max_per_hour:
        raise Refused(f"rate limit: {len(recent)} claims in the last hour (max {max_per_hour})")
    for other_issue, rec in state["claims"].items():
        if rec.get("state") not in ACTIVE_STATES:
            continue
        clash = overlapping(files, rec.get("files") or [])
        if clash:
            shown = ", ".join(f"{g} ~ {o}" for g, o in clash[:5])
            raise Refused(f"file scope overlaps the active claim on issue {other_issue}: {shown}")

    previous = state["claims"].get(str(issue))
    if previous is not None:
        state["history"] = (state.get("history", []) + [previous])[-HISTORY_LIMIT:]
    stamp = now.isoformat()
    rec = {
        "issue": issue, "actor": actor, "event": event, "agent": agent, "branch": branch,
        "pr": None, "state": "claimed", "retries": 0, "budget_usd": round(budget_usd, 2),
        "spent_usd": 0.0, "commit": None, "files": files, "note": "", "outcome": None,
        "claimed_at": stamp, "updated_at": stamp, "released_at": None,
    }
    state["claims"][str(issue)] = rec
    state["claim_log"] = recent + [stamp]
    return rec


def attempt(state: dict, issue: int, *, max_retries: int = DEFAULT_MAX_RETRIES,
            now: datetime | None = None) -> dict:
    now = now or _now()
    _refuse_if_paused(state, "new attempts")
    rec = _require_active(state, issue)
    if rec.get("retries", 0) >= max_retries:
        raise Refused(f"issue {issue} has used {rec.get('retries', 0)} of {max_retries} retries; "
                      "escalate to a human instead of retrying")
    budget = rec.get("budget_usd") or 0.0
    if budget > 0 and (rec.get("spent_usd") or 0.0) >= budget:
        raise Refused(f"issue {issue} has spent ${rec.get('spent_usd', 0):.2f} of its "
                      f"${budget:.2f} budget estimate")
    rec["retries"] = rec.get("retries", 0) + 1
    rec["updated_at"] = now.isoformat()
    return rec


def update(state: dict, issue: int, /, *, now: datetime | None = None, **fields) -> dict:
    """Update an active claim's fields (positional-only `state`, so a `state=` field works)."""
    now = now or _now()
    rec = _require_active(state, issue)
    if "state" in fields and fields["state"] not in ACTIVE_STATES:
        raise Refused(f"state must be one of {', '.join(ACTIVE_STATES)} (use `release` to finish)")
    for key in ("budget_usd", "spent_usd"):
        if key in fields and fields[key] < 0:
            raise Refused(f"{key} must not be negative")
    rec.update(fields)
    rec["updated_at"] = now.isoformat()
    return rec


def release(state: dict, issue: int, *, outcome: str = "integrated", note: str = "",
            now: datetime | None = None) -> dict:
    now = now or _now()
    rec = _require_active(state, issue)
    if outcome not in OUTCOMES:
        raise Refused(f"outcome must be one of {', '.join(OUTCOMES)}")
    rec.update(state=RELEASED, outcome=outcome, released_at=now.isoformat(), updated_at=now.isoformat())
    if note:
        rec["note"] = note
    return rec


def pause(state: dict, reason: str = "", now: datetime | None = None) -> None:
    state.update(paused=True, paused_reason=reason, paused_at=(now or _now()).isoformat())


def resume(state: dict) -> None:
    state.update(paused=False, paused_reason="", paused_at=None)


# --------------------------------------------------------------------------- #
# Storage                                                                     #
# --------------------------------------------------------------------------- #
def load(path: str) -> dict:
    if not os.path.exists(path):
        return empty_state()
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise LedgerError(f"can't read the ledger at {path} ({e}); fix or move it, nothing was changed")
    if not isinstance(data, dict) or not isinstance(data.get("claims"), dict):
        raise LedgerError(f"the ledger at {path} is malformed; fix or move it, nothing was changed")
    base = empty_state()
    base.update(data)
    return base


def save(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"  # per-process temp file: concurrent writers can't collide
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, path)  # atomic: readers never see a half-written ledger


@contextlib.contextmanager
def locked(path: str):
    """Hold an exclusive lock for a whole read-modify-write, so concurrent claims serialize."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(f"{path}.lock", "a") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_UN)


def transact(path: str, fn):
    """Load, apply `fn(state)`, save. A Refused or LedgerError leaves the file untouched."""
    with locked(path):
        state = load(path)
        result = fn(state)
        save(path, state)
        return result


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def _fmt(rec: dict) -> str:
    pr = f"#{rec['pr']}" if rec.get("pr") else "-"
    budget = f"${rec.get('budget_usd') or 0:.2f}" if rec.get("budget_usd") else "none"
    commit = (rec.get("commit") or "-")[:12]
    lines = [
        f"#{rec['issue']:<4} {rec.get('state', '?').upper():<10} agent {rec.get('agent') or '-'}  "
        f"branch {rec.get('branch') or '-'}  PR {pr}",
        f"      retries {rec.get('retries', 0)}  budget {budget} (spent ${rec.get('spent_usd') or 0:.2f})  "
        f"commit {commit}  by {rec.get('actor') or '-'} via {rec.get('event') or '-'}",
        f"      files: {', '.join(rec.get('files') or []) or '(none declared)'}",
    ]
    if rec.get("outcome"):
        lines.append(f"      outcome: {rec['outcome']} at {rec.get('released_at')}")
    if rec.get("note"):
        lines.append(f"      note: {rec['note']}")
    return "\n".join(lines)


def status_text(state: dict, issue: int | None = None, show_all: bool = False,
                now: datetime | None = None) -> str:
    now = now or _now()
    if state.get("paused"):
        head = f"Ledger: PAUSED since {state.get('paused_at')} ({state.get('paused_reason') or 'no reason'})"
    else:
        head = "Ledger: open"
    recs = sorted(state["claims"].values(), key=lambda r: r.get("issue", 0))
    if issue is not None:
        recs = [r for r in recs if r.get("issue") == issue]
    elif not show_all:
        recs = [r for r in recs if r.get("state") in ACTIVE_STATES]
    active = sum(1 for r in state["claims"].values() if r.get("state") in ACTIVE_STATES)
    recent = len(_recent_claims(state, now))
    lines = [head, f"Active claims: {active}   claims in the last hour: {recent}", ""]
    lines += [_fmt(r) for r in recs] or ["(no matching claims)"]
    return "\n".join(lines)


def _money(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text}")
    if value < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return value


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", default=DEFAULT_LEDGER, help="ledger file (default: .progress/ledger.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("claim", help="claim an issue (refuses duplicates, overlaps, rate limit, pause)")
    c.add_argument("issue", type=int)
    c.add_argument("--agent", required=True, help="owner agent (type or name)")
    c.add_argument("--branch", required=True, help="agent/<issue>-<slug>")
    c.add_argument("--files", default="", help='owned globs, comma-separated: "tools/x.py,frontend/src/**"')
    c.add_argument("--actor", default="lead", help="who triggered the work (user, lead, workflow)")
    c.add_argument("--event", default="manual", help="what triggered it (manual, issue_comment, ...)")
    c.add_argument("--budget", type=_money, default=0.0, help="USD estimate for Claude usage (0 = none)")
    c.add_argument("--max-per-hour", type=int, default=DEFAULT_MAX_PER_HOUR)

    a = sub.add_parser("attempt", help="record one more repair loop (refuses past --max-retries)")
    a.add_argument("issue", type=int)
    a.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)

    u = sub.add_parser("update", help="update an active claim")
    u.add_argument("issue", type=int)
    u.add_argument("--state", choices=ACTIVE_STATES)
    u.add_argument("--pr", type=int)
    u.add_argument("--commit")
    u.add_argument("--branch")
    u.add_argument("--agent")
    u.add_argument("--budget", type=_money, dest="budget_usd")
    u.add_argument("--spent", type=_money, dest="spent_usd", help="USD spent so far (estimate)")
    u.add_argument("--note")

    r = sub.add_parser("release", help="finish an active claim")
    r.add_argument("issue", type=int)
    r.add_argument("--outcome", choices=OUTCOMES, default="integrated")
    r.add_argument("--note", default="")

    p = sub.add_parser("pause", help="global pause: refuse every new claim and attempt")
    p.add_argument("--reason", default="")
    sub.add_parser("resume", help="lift the global pause")

    s = sub.add_parser("status", help="show claims")
    s.add_argument("issue", type=int, nargs="?")
    s.add_argument("--all", action="store_true", help="include released claims")
    s.add_argument("--json", action="store_true")
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    path = a.ledger
    try:
        if a.cmd == "status":
            with locked(path):
                state = load(path)
            if a.json:
                recs = state["claims"] if a.issue is None else {
                    k: v for k, v in state["claims"].items() if k == str(a.issue)}
                print(json.dumps({"paused": state["paused"], "paused_reason": state["paused_reason"],
                                  "claims": recs}, indent=1, sort_keys=True))
            else:
                print(status_text(state, a.issue, a.all))
            return 0
        if a.cmd == "claim":
            rec = transact(path, lambda st: claim(
                st, a.issue, agent=a.agent, branch=a.branch, files=parse_globs(a.files), actor=a.actor,
                event=a.event, budget_usd=a.budget, max_per_hour=a.max_per_hour))
            print(f"claimed issue {a.issue} for {rec['agent']} on {rec['branch']}")
        elif a.cmd == "attempt":
            rec = transact(path, lambda st: attempt(st, a.issue, max_retries=a.max_retries))
            print(f"issue {a.issue}: attempt {rec['retries']} of {a.max_retries}")
        elif a.cmd == "update":
            fields = {k: getattr(a, k) for k in ("state", "pr", "commit", "branch", "agent", "budget_usd",
                                                 "spent_usd", "note") if getattr(a, k) is not None}
            rec = transact(path, lambda st: update(st, a.issue, **fields))
            print(f"issue {a.issue}: updated {', '.join(fields) or 'nothing'} (state {rec['state']})")
        elif a.cmd == "release":
            transact(path, lambda st: release(st, a.issue, outcome=a.outcome, note=a.note))
            print(f"issue {a.issue}: released ({a.outcome})")
        elif a.cmd == "pause":
            transact(path, lambda st: pause(st, a.reason))
            print("ledger paused: new claims and attempts are refused until `resume`")
        elif a.cmd == "resume":
            transact(path, resume)
            print("ledger resumed")
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    except LedgerError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
