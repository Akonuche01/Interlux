# Per-agent namespacing plan (Interlux daemon)

Status: design accepted 2026-09-29. Implements NOT started.
Depends on: docs/AGENT_AUTH_PLAN.md (step 1 — socket→agent identity).

## Why

Auth without isolation is theater: any approved agent could read,
steer, or listen to any other agent's threads, reuse its tools and
grants, and see its broadcasts. Namespacing makes each agent's world
private by construction. Provider credentials stay shared for now
(step 3); quotas are step 4.

## Identity plumbing

- `handle_request` already receives `client_socket`. Serve keeps
  `client_agents: {socket: agent_id}`, set at `initialize` once auth
  verifies, cleared on disconnect.
- Every handler below resolves the caller first; unknown sockets keep
  step-1 behavior (`capabilities` + pairing only).

## Threads

- State files gain an `agent` field, stamped at creation
  (`thread/resume` new, `fork`, `import`).
- `thread/list` filters to caller-owned threads.
- `thread/read`, `steer`, `compact`, `fork`, `archive`, `name/set`,
  `memories`, turn paths: refuse cross-agent ids with the same
  `-32602 unknown thread` the engine already uses (indistinguishable
  from missing — no oracle for enumerating others' ids).
- Migration: on upgrade, all unstamped legacy threads are stamped to
  the first paired agent (in practice Kara). No history goes dark,
  pins and indexes keep working, and there is no ambiguous
  claim-on-touch race.

## Tools

- Client-tool registrations already carry an owner socket; extend the
  owner to the agent id.
- Name-taken and re-register rules become per-agent: two agents may
  each own an `open_url`; globals remain shared and first-come.
- `tools` RPC shows globals plus the caller's own client tools only.

## Policy

- `policy.json` keeps global `default_provider/default_model/sandbox/
  approval_mode` as DEFAULTS.
- New per-agent section holds that agent's overrides plus its grants.
  Resolution: agent override wins, else global.
- Thread-scoped standing grants need no change: threads are owned, so
  grants inherit isolation automatically.

## Audit

- Every appended entry gains `agent_id`. Old entries without it stay
  readable; new readers must tolerate both.

## Broadcasts

- New `emit(payload, thread_id=None)`: events naming a thread go only
  to that thread owner's live sockets; global events
  (`mcpServer/*`, `skills/changed`, server lifecycle) still go to all.
- Per-agent socket sets maintained alongside `client_agents`.
- A second connection from the same agent is the same agent: it gets
  everything its agent may see (multi-device stays working).

## Out of scope (explicit)

- Step 3: per-agent provider credential namespaces (today a shared
  key's existence is visible to all agents).
- Step 4: quotas/fairness across agents.

## Test plan (for the implementation step)

- Two fake agents, adversarial: cross read/write/list/steer/compact
  all refuse; neither receives the other's broadcasts; global events
  reach both.
- Legacy migration: pre-upgrade threads land owned by agent one, pins
  resolve, drawer counts unchanged.
- Tool collision: same client-tool name registered by both agents
  works independently each way.
- Policy: agent override wins; deleting it falls back to global.
