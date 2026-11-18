---
name: fix-ci
description: Diagnose a failing CI run on an agent branch from its logs, fix the root cause, and push one commit.
---

# Fix CI

1. Read the failing job's log (`gh run view <id> --log-failed`). Find the first real error.
2. Reproduce locally with the same command CI runs.
3. Fix the underlying cause on the PR's branch. Never skip, delete or weaken a test or a
   lint rule to get green, and never change risk/execution behavior to make a test pass.
4. Push one commit. Stop after 3 attempts on the same failure and ask for help with what
   you tried.
