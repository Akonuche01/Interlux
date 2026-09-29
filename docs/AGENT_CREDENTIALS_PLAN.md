# Per-agent provider credentials plan (Interlux daemon)

Status: design accepted 2026-09-29. Implements NOT started.
Depends on: docs/AGENT_AUTH_PLAN.md (step 1 — identity),
docs/AGENT_NAMESPACING_PLAN.md (step 2 — isolation primitives).

## Why

Provider API keys are the most sensitive data the daemon holds, and
today they live in one global file served (masked, but present) to
every connected client. On a multi-agent engine that is a leak by
architecture: any approved agent learns which providers are keyed and,
worse, shares spending. Credentials must follow the same isolation as
threads and tools.

## Shape (single file, split secrecy)

`~/.interlux/agent/providers.json` keeps its atomic tmp+replace
discipline and its current top level for NON-secrets only:

- `base_url`, `models` overrides, tool sections' non-secret fields —
  global, visible to all agents. None of this is sensitive.
- API keys move to `agents: { <agent_id>: { <name>: {api_key} } }`,
  one section per agent. No agent can read, list, or infer another
  agent's section; unknown sections are indistinguishable from absent.

`INTERLUX_PROVIDERS` env override stays as the admin/global layer and
is documented as such (it bypasses per-agent scoping by design, for
single-operator setups).

## Resolution rules

- `api_key` resolves ONLY from the caller's own agent section. There
  is no global fallback and no cross-agent fallback. Ever.
- `base_url`, `models` lists resolve agent section → global section →
  built-in `DEFAULT_BASES`/`CURATED_MODELS`.
- Two agents may share one endpoint (e.g. tokenharbor base) while each
  spends its own key. Sharing an endpoint is not sharing a secret.

## RPC behavior (shapes unchanged — no client changes needed)

- `providers` (read): union of known adapters + configured blocks, as
  today, but `has_key`/`key_hint` are computed against the CALLER's
  key only. Another agent's keyed provider reads `has_key: false`.
- `providers` (write/delete api_key/base_url): lands in the caller's
  own section automatically, scoped by socket identity. Her existing
  `setProviderKey`/delete flows work unchanged.
- `model/list`, turns: `make_provider`/`list_models` authenticate with
  the caller's key only. An unkeyed (for you) provider lists from
  config/curated and fails turns loudly at request time, as today.

## Tool sections

`web_search` and future tool sections follow the same split: their
`api_key` fields move under the agent section with the identical
resolver; the rest of each section stays global admin config.

## Migration

Existing global `api_key` values move wholesale into the first paired
agent's section (same rule as threads: Kara, in practice). Base URLs
and model lists stay global. The migration logs what moved (names
only, never values). After migration no `api_key` remains at top
level; a startup check warns loudly if one ever reappears there.

## Out of scope (explicit)

- Step 4: quotas/fairness across agents (spending caps per agent key).
- Central key escrow / rotation UX (a future Interlux screen, not this).

## Test plan (for the implementation step)

- Two agents, adversarial: A sees `has_key: false` (and no hint) for a
  provider only B keyed; B's turns spend B's key (verified by stubbing
  the HTTP layer and asserting the Authorization header per agent).
- Independent configure/delete/re-add by one agent is invisible to the
  other in reads, rosters, and turns.
- Migration: pre-upgrade global keys land under agent one, URLs stay
  global, top level holds no `api_key` afterwards.
- Audit/log sweep: no key material in logs, audit trail, broadcasts,
  or error messages (assert on redacted fixtures).
