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
- Docs (`README.md`, `CLAUDE.md`, `.env.example`) are written by the lead at integration, not by
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

## 3. Launch agents
One Agent call per issue, all in one message, `isolation: "worktree"`, in the background. Every
brief states: branch name; owned and forbidden files; migration revision; its test database;
the shared venv path; no network calls to Alpaca/Anthropic/SEC/FRED; no push, PR or issue
comments; never place orders; and the progress milestones it must report with:
```bash
python3 "/Users/bradycai/Stock Application/tools/agent_progress.py" update <issue> <percent> working "<step>"
```
(`done` at 100% when committed, `blocked` with a reason when a decision is needed.)

## 4. Integrate each finished agent (lead)
1. Read the diff — especially risk, execution, broker, automation and migration files.
2. Re-run `ruff check app tests migrations`, `pytest -q` with `TEST_DATABASE_URL`, and
   `npm run build` in the agent's worktree.
3. Live read-only check where mocks can't prove it (market data, streams) — never orders.
4. Add docs, scan the new commits for secret values, push, open a draft PR closing the issue.
5. `python3 tools/agent_progress.py integrate <issue> "<what was verified>"`.
6. Get an independent review (qa-security agent) for anything touching money movement.

## 5. Finish
When every issue is integrated: record them with `tools/agent_progress.py finished "<line>"`,
confirm CI on every PR, drop the per-agent test databases, and report to the user.
