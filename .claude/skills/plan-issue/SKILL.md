---
name: plan-issue
description: Turn a feature request into one or more scoped GitHub issues with acceptance criteria, file ownership and test requirements.
---

# Plan an issue

1. Read `CLAUDE.md` and the code the request touches. Identify the smallest change that
   delivers the request.
2. Split work so each issue can be implemented and reviewed on its own branch
   (`agent/<issue>-<slug>`) without two agents editing the same file.
3. For each issue write: scope, out-of-scope, files it may change, interfaces it depends
   on, acceptance criteria, required tests, and whether it touches risk/execution,
   migrations, permissions or deployment (these need human approval to merge).
4. Order the issues by dependency. Do not start implementation in this skill.
