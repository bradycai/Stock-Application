---
name: run-agent-workflow
description: Run several roadmap issues in parallel with one agent per issue in isolated git worktrees, tracked live in PROGRESS.txt, then review, verify and integrate each result.
---

# Run the parallel agent workflow

The lead (this session) plans, launches, tracks and integrates. Agents implement. Progress is
visible to the user in `PROGRESS.txt` at the repository root, refreshed every minute.

## 1. Plan
- Pick issues that can run at the same time with **disjoint file ownership**. Shared files
  (`app/models.py`, `app/config.py`, `app/main.py`) are allowed only as appends in a named block
  at an anchor no other agent uses. Never let two agents edit the same function.
- Migrations: give each agent its own revision id; if two branch from the same head, the lead
  re-chains the later one at integration (`down_revision`).
- Docs (`README.md`, `AGENTS.md`, `.env.example`) are written by the lead at integration, not by
  agents — agents list new settings in their report.
- Create a GitHub issue per item (scope, files, acceptance criteria) and one disposable test
  database per agent: `createdb trading_assistant_test_<x>`.

## 2. Start the board
```bash
python3 tools/agent_progress.py reset
python3 tools/agent_progress.py seed <issue> --title "<title>" --branch agent/<issue>-<slug> --agent <type>
python3 tools/agent_progress.py watch --interval 60     # run in the background
open -b com.microsoft.VSCode PROGRESS.txt              # or any editor that reloads changed files
```

## 2b. Claim each issue in the ledger (before launching)
`tools/agent_ledger.py` is the dedup and runaway guard (local state in `.progress/ledger.json`,
gitignored). Claim every issue **before** its agent starts, with the same file ownership the
brief gives it:
```bash
python3 tools/agent_ledger.py status                   # paused? already claimed?
python3 tools/agent_ledger.py claim <issue> --agent <type> --branch agent/<issue>-<slug> \
    --files "<owned globs, comma-separated>" --budget <usd estimate> --actor <who asked> --event manual
```
A claim is refused (exit 1, nothing written) when the issue already has an active claim, its
globs overlap another active claim, more than `--max-per-hour` (default 6) claims were made in
the last hour, or the ledger is paused. **Do not launch an agent whose claim was refused**:
resolve the conflict (narrow the file scopes, wait, or release the stale claim) first.
`python3 tools/agent_ledger.py pause --reason "<why>"` stops every new claim and repair attempt
at once (e.g. a cost spike or a bad merge); `resume` lifts it. `update` and `release` still work
while paused, so in-flight work can be wound down.

## 3. Launch agents
One Agent call per issue, all in one message, `isolation: "worktree"`, in the background. Every
brief states: branch name; owned and forbidden files; migration revision; its test database;
the shared venv path; no network calls to Alpaca/Anthropic/SEC/FRED; no push, PR or issue
comments; never place orders; and the progress milestones it must report with:
```bash
python3 "/Users/bradycai/Stock Application/tools/agent_progress.py" update <issue> <percent> working "<step>"
```
(`done` at 100% when committed, `blocked` with a reason when a decision is needed.)
Then mark the claim as running: `python3 tools/agent_ledger.py update <issue> --state working`.

## 4. Integrate each finished agent (lead)
0. **Every repair loop counts.** Before sending an agent back to fix review findings or a failing
   check (including `fix-ci`), record it: `python3 tools/agent_ledger.py attempt <issue>`. It is
   refused after `--max-retries` (default 3) attempts or once the recorded spend reaches the
   budget (`update <issue> --spent <usd>`). On a refusal, stop: mark the issue `blocked` on the
   board and `update <issue> --state blocked --note "<why>"`, and ask the user.
1. Read the diff — especially risk, execution, broker, automation and migration files.
2. Re-run `ruff check app tests migrations`, `pytest -q` with `TEST_DATABASE_URL`, and
   `npm run build` in the agent's worktree.
3. Live read-only check where mocks can't prove it (market data, streams) — never orders.
4. Add docs, scan the new commits for secret values, push, open a draft PR closing the issue.
5. `python3 tools/agent_progress.py integrate <issue> "<what was verified>"`, then close the claim:
   `python3 tools/agent_ledger.py update <issue> --pr <n> --commit <sha> --spent <usd>` and
   `python3 tools/agent_ledger.py release <issue> --outcome integrated`. A dropped issue is
   released with `--outcome abandoned` (or `failed`), which frees its files for another claim.
5b. If the PR touches no CODEOWNERS-protected file and targets `Production`, mark it ready and
   add the `automerge` label (`gh pr ready <n> && gh pr edit <n> --add-label automerge`); check
   with `python3 tools/automerge_policy.py --pr <n>`. Protected PRs wait for the user.
6. Get an independent review (qa-security agent) for anything touching money movement.

## 5. Finish
When every issue is integrated: record them with `tools/agent_progress.py finished "<line>"`,
confirm `python3 tools/agent_ledger.py status` shows no active claims left, confirm CI on every
PR, drop the per-agent test databases, and report to the user.
