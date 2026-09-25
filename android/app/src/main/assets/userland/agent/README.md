# Interlux Agent Daemon — P1

## Files

| File | Purpose |
|---|---|
| `serve.py` | Main daemon (session, transport, RPC, approvals) |
| `transport.py` | WebSocket JSON-RPC server |
| `audit.py` | JSONL transcript |
| `providers/` | OpenAI + Anthropic + Inception + Token Harbor + local (llama-server) adapters (stdlib `urllib`, no extra deps; `model` flows per-turn) |
| `tools/` | shell/exec/pty/fs/git/pkg + extensible registry |
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
| `approve` | `{"params": {"id": "...", "decision": "accept"}}` | `{"result": true}` |
| `tools_refresh` | `{"id": 4}` | `{"result": {"loaded": [...], "tools": [...]}}` |
| `cancel` | `{"params": {"thread_id": "..."}}` | `{"result": true}` + `cancelled` broadcast (kills tracked procs) |

Notifications:
- `approval_request` → `{"id": "...", "params": {"command": "..."}}`
- `complete` / `error`

## Next (P2)
- PATH/plugin tool registry, pty-attach to live tabs
- (done) True streaming (chunked SSE) over stdlib `urllib`, fs/git/pkg/pty tools
