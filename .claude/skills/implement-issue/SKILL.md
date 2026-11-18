---
name: implement-issue
description: Implement one GitHub issue on a fresh branch from Production (the default branch), with tests, and open a draft PR.
---

# Implement an issue

1. Confirm there is no existing branch or open PR for the issue (`gh pr list --search`).
2. Branch from the latest `Production` (the default branch): `agent/<issue>-<slug>`.
3. Implement only the issue's scope. Follow `CLAUDE.md` safety boundaries. When running in the
   parallel workflow, report each milestone with `tools/agent_progress.py update` so it shows in
   `PROGRESS.txt`.
4. Add tests for new behavior and its failure paths. Run `ruff check app tests` and
   `pytest -q` (backend) and `npm run build` (frontend, if changed).
5. Commit with a message that references the issue. Push the branch and open a **draft**
   PR whose body lists: summary, tests run, risk/execution impact, migration notes, cost
   impact, rollback steps, and `Closes #<issue>`.
6. Ask the qa-security agent to review the exact diff. Never merge or deploy.
