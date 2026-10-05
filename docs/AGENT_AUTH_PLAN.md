# Agent auth & pairing plan (Interlux daemon)

Status: design accepted 2026-09-29. Implements NOT started.

## Why

Interlux is a neutral multi-agent engine, not one client's backend. Today any
process on loopback can use the daemon as any agent: no identity, no
approval, global broadcasts, one shared policy space. The signature-level
`CONTROL_AGENT` permission further binds our client apps to our release key,
which blocks third-party agents entirely. This plan replaces both with
user-approved pairing plus per-agent tokens.

Non-goals for this step: per-agent namespacing of threads/tools/policy
(step 2), broadcast scoping (step 2), quotas (later).

## Rules the design must satisfy

1. Daemon listens on 127.0.0.1 only. No remotely reachable surface, ever.
2. Any agent app can connect; nothing works until the user approves it.
3. Approval is revocable at any time, from Interlux UI.
4. No shared release keys. Third parties never need our signing key.
5. Secrets: hashes on disk, raw secret shown once, never in logs/audit.
6. Existing client installs migrate with a single approve tap.

## Pairing flow

1. Unknown app opens a websocket and calls `initialize` without (or with
   an unknown) `auth`.
2. Daemon replies `{"pairing_required": true, "code": "<short-code>"}` and
   records a pending pairing (app name resolved via PackageManager,
   timestamp). Nothing else on that connection is honored except
   `capabilities` (read-only self-description).
3. User opens Interlux → Agents screen → sees pending request with the
   real app name + code → taps Approve (or Deny).
4. On approve the daemon mints `agent_id` (stable, e.g. `ag_<rand>`) and a
   secret token, stores `{agent_id, app package, label, sha256(token),
   created}` in `~/.interlux/agent/agents.json` (wipe-proof home, same
   rule as providers.json/policy.json), and returns `{agent_id, token}`
   once, over the pairing connection.
5. Afterwards the client sends `auth: {agent_id, token}` inside every
   `initialize`. The daemon maps the connection to the agent id; unknown
   or revoked credentials get `pairing_required` again.

## Revocation

Agents screen lists paired agents (label, package, created, last seen).
Delete removes the entry and drops that agent's live connections. Next
connect from that app restarts the pairing flow from scratch.

## What the signature permission becomes

`CONTROL_AGENT` (signature level) stays ONLY for engine lifecycle:
start/stop/restart the daemon itself. Everything an agent does
day-to-day (turns, tools, threads, policy, providers, bots) moves to
token auth. Rationale: lifecycle affects every agent on the device, so
it stays privileged; usage must be open or third parties can never run.

## Migration

- Current clients (no token): first `initialize` after upgrade returns
  `pairing_required`; user taps approve once in Interlux; the stored
  token is used from then on. No re-install, no key sharing.
- Daemon with no `agents.json`: behaves as today for... nothing. There
  is no legacy bypass: unknown connections get `pairing_required`.
  (Single-user device; the one tap is the whole cost.)

## Threat model (accepted limits)

- A malicious on-device app gets zero capability without a user tap.
  Pairing screen shows the real package name; no silent grants.
- A rooted phone could sniff loopback bearer tokens. Accepted and
  documented: same exposure class as every local pairing scheme
  (Bluetooth, adb auth). Rotation = revoke + re-pair.
- Token theft scope is one agent's namespace (after step 2), never the
  engine itself; lifecycle stays behind the signature wall.

## Test plan (for the implementation step)

- Unknown client gets `pairing_required`, and nothing else works.
- Approve → token works across reconnects and daemon restarts.
- Revoke → live connections drop, next connect re-prompts.
- Wrong/unknown token → `pairing_required`, no crash, no leak.
- `agents.json` holds hashes only; logs/audit contain no secrets.
