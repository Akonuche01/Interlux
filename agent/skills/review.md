---
name: review
description: Review unstaged changes the structured way - diff, model critique, severity-ranked findings. Use when asked to review code, and run review turns in plan mode.
tools: review, git_diff
---

## Flow

1. Run the `review` tool with the repo path (`cwd`). It diffs and critiques
   in one read-only call (no approval needed) and returns structured findings.
2. For raw material first: `git_diff` / `git_status` / `git_log` (all read-only).
3. Present findings grouped by severity, then a one-line verdict.
4. Never auto-fix on a review turn unless asked. Review turns SHOULD run in
   `mode: "plan"` so no write tool can fire accidentally.

## Findings schema

```text
SUMMARY: <one line verdict>
- [severity] path/to/file.py:LINE short issue -> concrete suggestion
```

Severity is info|minor|major|critical. Skip style nits and praise.
