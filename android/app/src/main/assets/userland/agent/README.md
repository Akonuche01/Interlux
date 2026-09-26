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
| `turn` | `{"params": {"user": "...", "provider": "openai"}}` | `{"result": {"turn_id": "..."}}` |
| `turn` + images | `{"params": {"user": "...", "images": ["<path or data: URL>"]}}` | same, model sees the images |
| `approve` | `{"params": {"id": "...", "decision": "accept", "scope": "turn"\|"session"}}` | `{"result": true}` |
| `policy` | `{"params": {}}` or `{"params": {"sandbox": "read-only"}}` or `{"params": {"revoke": "<thread>"\|"*"}}` | `{"result": {"policy": {...}, "path": "...", "revoked": n}}` |
| `tools_refresh` | `{"id": 4}` | `{"result": {"loaded": [...], "tools": [...]}}` |
| `cancel` | `{"params": {"thread_id": "..."}}` | `{"result": true}` + `cancelled` broadcast (kills tracked procs) |
| `thread/resume` | `{"params": {"thread_id": "..."}}` | `{"result": {"thread_id", "source": "state"\|"audit"\|"new", "turns", "base", "history", "memories", "path"}}` |
| `thread/fork` | `{"params": {"thread_id": "...", "to": "optional"}}` | `{"result": {"thread_id", "from", "turns", "history_len", "base", "path"}}` |
| `thread/compact` | `{"params": {"thread_id": "...", "provider": "...", "model": "..."}}` | `{"result": {"thread_id", "summary", "turns_before", "path"}}` |
| `memories` | `{"params": {"thread_id": "...", "content": "..."}}` (omit `content` to read) | `{"result": {"thread_id", "memories", "path"}}` |
| `skills` | `{"params": {}}` or `{"params": {"load": "<name>"}}` or `{"params": {"refresh": true}}` | `{"result": {"skills": [...], "user_dir": "..."}}` or full skill `body` |

Turn params also take `sandbox` (`full` default | `workspace` | `read-only`)
and `sandbox_root` (workspace confinement root, default `$HOME`).

Notifications:
- `approval_request` → `{"id": "...", "params": {"command": "..."}}`
- `complete` / `error`

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

## Next (P2)
- PATH/plugin tool registry, pty-attach to live tabs
- (done) True streaming (chunked SSE) over stdlib `urllib`, fs/git/pkg/pty tools
