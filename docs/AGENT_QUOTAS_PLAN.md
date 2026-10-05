# Quotas and fairness plan (Interlux daemon)

Status: design accepted 2026-09-29. Implements NOT started.
Depends on: docs/AGENT_AUTH_PLAN.md (step 1 — identity),
docs/AGENT_NAMESPACING_PLAN.md (step 2 — isolation),
docs/AGENT_CREDENTIALS_PLAN.md (step 3 — key isolation).

## Standing rule

The owner agent (first paired, flagged `owner` in
`agents.json`) is EXEMPT from every limit below: no concurrency cap,
no token budget, no turn ceiling beyond the existing global backstop.
Exempt means exempt, not "high limit": no future default may catch
her. Quotas constrain third-party agents only. Her access is
everything she has today — every RPC, every tool, full policy scope,
bots included.

## Why

Keys are per-agent after step 3, so spending is separated. What
remains shared is daemon resources: the asyncio loop (one agent's
concurrent turns can starve others), shared-IP rate limits (one
agent's burst 429s everyone on the same endpoint), and memory held by
histories. Unbounded third parties can therefore degrade the owner.

## Design

- **Per-agent concurrency cap** (default 4, owner exempt): excess
  `turn` starts are refused with a typed `quota exceeded` error, not
  queued silently.
- **Token/window budgets**, admin-set per agent
  (`agents.<id>.quota`), default unlimited, owner exempt. Accrual
  hooks into the per-turn usage already broadcast; over-budget turns
  truncate with a note instead of dying mid-word.
- **Turn ceiling** stays global (existing backstop), overridable per
  agent, never applied to the owner.
- **No priority scheduler**: FIFO plus caps. No preemption, no cross-
  agent starvation games to debug.
- **Observability**: usage events carry `agent_id` (rides on step 2's
  audit tagging) so an admin screen can show per-agent spend.
- **Back-compat**: nothing configured means exactly today's behavior.
  Every enforcement point fails as a loud typed refusal, never a
  silent stall.

## Out of scope (explicit)

- Provider-side rate smoothing (their 429s are theirs to manage).
- Background-vs-interactive priority.
- Central key escrow / rotation UX.

## Test plan (for the implementation step)

- Owner agent: no cap path is reachable (assert exemption at every
  enforcement point, including future defaults).
- Third party at cap: refused with typed error while owner and idle
  third parties proceed.
- Budget overrun truncates mid-turn with a note; usage stays tagged.
- Unconfigured parity: byte-identical behavior to today.
