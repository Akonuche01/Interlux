# Interlux Agent Daemon — P1

## Files

| File | Purpose |
|---|---|
| `serve.py` | Main daemon (session, transport, RPC, approvals) |
| `transport.py` | WebSocket JSON-RPC server |
| `audit.py` | JSONL transcript |
| `policy.py` | Epic B: sandbox modes + standing grants (`~/.interlux/agent/policy.json`, wipe-proof) |
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

## Next (P2)
- PATH/plugin tool registry, pty-attach to live tabs
- (done) True streaming (chunked SSE) over stdlib `urllib`, fs/git/pkg/pty tools
