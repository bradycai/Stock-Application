---
name: review-pr
description: Review a pull request's exact diff against its acceptance criteria, with emphasis on trading safety.
---

# Review a PR

1. Fetch the diff (`gh pr diff <n>`) and the linked issue's acceptance criteria. Review
   the code, not the PR summary.
2. Apply the qa-security checklist (`.claude/agents/qa-security.md`).
3. Run the same checks as CI.
4. Post findings ranked by severity with file:line and a failure scenario. Request changes
   for anything that weakens risk checks, automation gates, or secret handling.
