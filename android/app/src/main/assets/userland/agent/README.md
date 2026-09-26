# Interlux Agent Daemon — P1

## Files

| File | Purpose |
|---|---|
| `serve.py` | Main daemon (session, transport, RPC, approvals) |
| `transport.py` | WebSocket JSON-RPC server |
| `audit.py` | JSONL transcript |
| `policy.py` | Epic B: sandbox modes + standing grants (`~/.interlux/agent/policy.json`, wipe-proof) |
| `threads.py` | Epic C: thread history/base/notes (`~/.interlux/agent/threads/*.json`, atomic writes, audit replay) |
| `skills.py` | Epic D: markdown skills (bundled `agent/skills/` + user `~/.interlux/agent/skills/`, catalog + `skill_load` tool) |
| `mcp.py` | Epic E: stdio MCP client — spawns servers from `~/.interlux/agent/mcp.json`, proxies tools as `mcp_<server>__<tool>` |
| `providers/` | OpenAI + Anthropic + Inception + Token Harbor + local (llama-server) adapters (stdlib `urllib`, no extra deps; `model` flows per-turn) |
| `tools/` | shell/exec/pty/fs/git/pkg/tabs/image_generate + registry |
| `plugins/` | Drop-in `*.py` tools (auto-load at boot, `tools_refresh` hot-loads) |

## Run

```bash
# Terminal 1: Start daemon
export OPENAI_API_KEY=sk-...
python3 -m agent -p 4600

# Terminal 2: Test client
python3 -m agent.test_kara
```

## Protocol

| Method | Request | Response |
|---|---|---|
| `capabilities` | `{"id": 1, "method": "capabilities"}` | `{"result": {"protocol": 1, "methods": [...], "providers": [...], "tools": [...]}}` |
| `turn` | `{"params": {"user": "...", "provider": "openai"}}` | `{"result": {"turn_id": "..."}}` (+ `usage` when the provider reports it, `rounds` always) |
| `turn` + images | `{"params": {"user": "...", "images": ["<path or data: URL>"]}}` | same, model sees the images |
| `approve` | `{"params": {"id": "...", "decision": "accept", "scope": "turn"\|"session"}}` | `{"result": true}` |
| `policy` | `{"params": {}}` or `{"params": {"sandbox": "read-only"}}` or `{"params": {"revoke": "<thread>"\|"*"}}` | `{"result": {"policy": {...}, "path": "...", "revoked": n}}` |
| `tools_refresh` | `{"id": 4}` | `{"result": {"loaded": [...], "tools": [...]}}` |
| `cancel` | `{"params": {"thread_id": "..."}}` | `{"result": true}` + `cancelled` broadcast (kills tracked procs) |
| `thread/resume` | `{"params": {"thread_id": "..."}}` | `{"result": {"thread_id", "source": "state"\|"audit"\|"new", "turns", "base", "history", "memories", "path"}}` |
| `thread/fork` | `{"params": {"thread_id": "...", "to": "optional"}}` | `{"result": {"thread_id", "from", "turns", "history_len", "base", "path"}}` |
| `thread/compact` | `{"params": {"thread_id": "...", "provider": "...", "model": "..."}}` | `{"result": {"thread_id", "summary", "turns_before", "path"}}` |
| `thread/list` | `{"params": {"limit": 50}}` | `{"result": {"threads": [{thread_id, turns, messages, created, updated, parent, has_base, has_memories, preview}]}}` (newest first, pure read) |
| `thread/read` | `{"params": {"thread_id": "...", "limit": 100}}` | `{"result": {"thread_id", "source", "turns", "base", "memories", "messages": tail, "total"}}` (pure read, never creates state) |
| `providers` | `{"params": {"provider": "..."}}` (get, masked) or `{"params": {"provider": "...", "api_key": "...", "base_url": "..."}}` (set) or `{"params": {"provider": "...", "delete": true}}` or `{}` (list all) | `{"result": {"provider", "base_url", "has_key", "key_hint"}}` — secrets inbound only, never on the wire or in audit |
| `turn/steer` | `{"params": {"thread_id": "...", "message": "...", "start": true}}` (+ optional turn params) | cancels the live turn, records the message, optionally starts a carrying turn |
| `tools` | `{"id": n, "method": "tools"}` | `{"result": {"tools": [...], "client_tools": [...], "mcp_tools": [...]}}` |
| `tools/register` | `{"params": {"name": "...", "description": "..."}}` | daemon calls the tool back out over your socket (`tool/call`) |
| `tools/unregister` | `{"params": {"name": "..."}}` | owner-only removal |
| `memories` | `{"params": {"thread_id": "...", "content": "..."}}` (omit `content` to read) | `{"result": {"thread_id", "memories", "path"}}` |
| `skills` | `{"params": {}}` or `{"params": {"load": "<name>"}}` or `{"params": {"refresh": true}}` | `{"result": {"skills": [...], "user_dir": "..."}}` or full skill `body` |
| `mcp` | `{"params": {}}` or `{"params": {"restart": true}}` | `{"result": {"servers": [{"name", "status", "tools", "error"}], "config": "...", "registered_tools": [...]}}` |

Turn params also take `sandbox` (`full` default | `workspace` | `read-only`)
and `sandbox_root` (workspace confinement root, default `$HOME`), plus
`mode` (`exec` default | `plan` — plan blocks write tools pre-approval,
like read-only but as declared intent), plus `max_rounds` (default 1 —
single provider call as always; higher runs the bounded agentic loop,
model sees tool results and continues, hard-capped at 10).

Notifications:
- `approval_request` → `{"id": "...", "params": {"command": "..."}}`
- `complete` / `error` (legacy terminal events; `complete` carries
  `turn_id` + `thread_id` + `tools`)
- `thread/started` (first turn of a new thread), `turn/started`
  (`turn_id`, `thread_id`, `mode`, `sandbox`, `provider`), `turn/completed`
  (`tools` run, `usage`, or `cancelled: true`)
- `item/started` + `item/completed` per tool call (`item_id`, `tool`,
  completion `status`: success|error|denied|blocked)
- `fs/changed` (`op`, `path`) on successful fs_write/fs_edit/image_generate
- `skills/changed` (current skill names) after skills refresh
- `mcpServer/started` + `mcpServer/stopped` (`server`, `tools`)
- `usage` → `{"turn_id": "...", "usage": {"prompt_tokens", "completion_tokens", "total_tokens"}}` (when reported)

## Sandbox & approval scopes (Epic B)

- `approve(scope="session")` writes a standing grant to `policy.json`
  (survives turns and daemon restarts): command-class tools
  (shell/exec/pty_run/pkg/tabs) store the **exact command label**;
  everything else (fs_write/fs_edit/image_generate/plugins) stores the
  **tool name**. `approve` without scope = one-shot (today's behavior).
- `policy` method: read/set the default sandbox, revoke grants
  (`revoke: "<thread>"` or `"*"`). Grants are also audited (`type: grant`).
- Sandboxes (per turn, default from `policy.sandbox`):
  - `full` — unchanged.
  - `workspace` — `fs_write`/`fs_edit`/`image_generate` confined under one
    root; relative paths re-root under it; escapes (`..`, absolute outside)
    rejected with `outside workspace root`.
  - `read-only` — write-class calls are blocked **before** the approval
    prompt (no theater), with an explicit error the model sees.
- Capabilities advertise `sandboxes` + `approve_scopes` + the `policy`
  method.

## Sessions with memory (Epic C)

- Thread state at `~/.interlux/agent/threads/<thread>.json` (wipe-proof,
  atomic tmp+replace): persistent turn counter, optional compacted `base`
  summary, and the full `[user, assistant, ...]` history. Completed turns
  append automatically — every `turn` on a thread sees prior turns, with
  tool output folded into the assistant messages.
- `thread/resume`: open a thread. `source: "state"` (canonical file),
  `"audit"` (state missing → rebuilt by replaying the JSONL audit tail),
  or `"new"` (fresh empty thread, idempotent).
- `thread/fork`: copy `base` + `history` + notes into a new thread
  (`parent` recorded, counter reset, later turns independent). Auto-names
  `<src>-fork[-N]` when `to` is omitted; refuses existing targets.
- `thread/compact`: provider summarizes the transcript into `base`,
  history resets, counter is kept, audit gets a `type: compact` marker
  (summary included). Needs an available `provider` (else error).
- `memories` / `<thread>.memories.md`: client-writable notes injected as
  a system message on every turn (after `base`, before history).
- Message order per turn: `base` summary → client notes → history →
  current user message. Turn ids are now `<thread>:<n>` with a real
  per-thread counter (cancel/approval flows unchanged for fresh threads).

## Skills (Epic D)

- Skill files are markdown with front-matter (`name`, `description`,
  optional `tools` advisory list, optional `tools_dir`):

  ```markdown
  ---
  name: alpine-guest
  description: when to use this (what the model reads)
  tools: shell
  tools_dir: tools
  ---
  body instructions ...
  ```

- Two locations: bundled `agent/skills/*.md` (shipped) and user/client
  `~/.interlux/agent/skills/*.md` (wipe-proof). User skills win name
  collisions. Bundled examples: `alpine-guest`, `pentest-tools`.
- Every turn injects a compact **catalog** system message (name +
  description for each skill). Full bodies are injected when the turn
  passes `params.skills` (list of names, or `"all"`), after notes and
  before history.
- The model can self-serve: the read-only **`skill_load`** tool returns a
  skill body (no approval, allowed in read-only sandbox); the result folds
  into thread history like any tool output.
- `skills` RPC: list summaries, `load` one (returns `body`), or `refresh`
  to re-read disk. `tools_refresh` also rescans skills.
- Skill tool plugins: `tools_dir` in front-matter points at a directory of
  `*.py` files loaded through the same `scan_plugins` machinery as drop-in
  plugins — same approval (`WRITE_TOOLS`), `EXTRA_WRITE`, and audit path.

## MCP servers (Epic E)

- Config at `~/.interlux/agent/mcp.json` (wipe-proof, optional):

  ```json
  {"servers": {"echo": {"command": ["python3", "-u", "server.py"],
                         "env": {}, "cwd": "..."}}}
  ```

- Transport is newline-delimited JSON-RPC 2.0 over stdio; handshake is
  `initialize` → `notifications/initialized` → `tools/list` (each side
  tolerates the other: unparseable stdout lines ignored, `ping` answered,
  unknown server requests refused).
- Server tools register as `mcp_<server>__<tool>` through the same
  registry/approval/audit path as native tools: they land in `EXTRA_WRITE`
  (always ask first, session-grantable), tool errors (`isError`) surface as
  `status: error`, results fold into thread history.
- Lifecycle: spawned at daemon boot, reconciled on every `tools_refresh`
  (new/restarted servers registered, removed ones unregistered), full
  restart via `mcp {"restart": true}`, stopped on shutdown. Failures
  (bad command, handshake timeout, server exit, stderr tail) are logged
  and visible in the `mcp` RPC — never silent.

## Web search + usage (Epic F)

- `web_search` tool (read-only, no approval): Tavily-compatible endpoint
  from a `web_search` section of the provider config
  (`{base_url, api_key, path?, extra?}` — never hardcoded). Without config
  it returns a clean error, not silence. Returns `{answer, results:
  [{title, url, snippet}]}`.
- Token usage: every provider normalizes to
  `{prompt_tokens, completion_tokens, total_tokens}` — captured from OpenAI
  stream chunks (`stream_options.include_usage`), Anthropic
  `message_start`/`message_delta`, Responses `response.completed`, and
  non-streamed full bodies. Surfaced three ways: live `usage` broadcast,
  `turn` result field, and the audit record. Never stored in thread
  history (kept out of model context).

## Plan mode + review flow (Epic G)

- Turn `mode: "plan"` ("look, don't touch"): write-class tools are blocked
  **before** the approval prompt with an explicit `plan mode` error, in any
  sandbox. `exec` (default) is unchanged. The mode is recorded on the turn
  (history + audit); capabilities advertise `modes: ["exec", "plan"]`.
- `review` tool (read-only, no approval): the review recipe executable —
  `git_diff(cwd)` → model critique → structured findings
  (`SUMMARY:` + `- [severity] file:LINE issue -> suggestion` lines).
  Returns `{status, repo, diff_stat, findings}`. Empty diff → clean verdict
  without spending a model call; unknown provider / git failure → explicit
  error. Critique runs one-shot (`stream: false`) and never broadcasts.
- `review` skill (bundled): the flow, the findings schema, and the rule to
  run review turns in `mode: "plan"`.
- Shared config lives in `pconfig.py` (`provider_config`, `config_section`,
  `make_provider`) so tools and the daemon read the same source.
- Device note: the userland git is Termux-built with a bogus baked-in
  system gitconfig path — `tools/git.py` disables it (`GIT_CONFIG_NOSYSTEM`
  + `GIT_CONFIG_SYSTEM=/dev/null`) for every git subprocess.

## Autonomous turn loop (Epic J)

- `max_rounds` per turn (default 1 = legacy single-pass). With N > 1 the
  daemon re-invokes the provider after each tool round, folding the round's
  model text + tool outcomes into the next round's context (`[assistant
  round N]: ...` + tool lines), until a tool-free answer or the cap (10).
- Approvals, sandbox/plan gates, usage capture, item events, and history
  all operate per round; history/audit record the whole turn once with
  `rounds` counted. Cancellation settles between rounds via the normal
  path. Empty "new" thread states are never persisted (keeps "unknown
  thread" errors repeatable and `thread/list` free of hollow entries).

## Provider keys, steering, client tools (Epic I.3)

- `providers` RPC: list sections, get one masked (`base_url`, `has_key`,
  `key_hint` — never the secret), set `api_key`/`base_url` (atomic write
  to wipe-proof `providers.json`, audited without secrets), delete a
  section. Refuses writes while `INTERLUX_PROVIDERS` env-pins the config.
- `turn/steer`: cancel the thread's live turn (waits for it to settle),
  record the steer message in history, and either stop (`start: false`)
  or run a new turn carrying it (turn params pass through). Unknown
  threads error; steering a live history-less thread works.
- Client tools: `tools/register` offers a tool the daemon calls back out
  to over your socket (`{"method": "tool/call", "params": {"name",
  "arguments"}}` → reply `{"id", "result"}`). Ask-first approval,
  audited, 30s timeout; dead owners fail loudly and auto-unregister;
  `tools/unregister` is owner-only; `tools` lists the registry split by
  origin (all / client / mcp).

## Next (P2)
- PATH/plugin tool registry, pty-attach to live tabs
- (done) True streaming (chunked SSE) over stdlib `urllib`, fs/git/pkg/pty tools
