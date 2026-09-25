# Interlux Agent Engine — Plan A

> **Positioning:** Interlux remains a pure terminal. The agent is a capability layer (daemon + protocol) that Kara or any client can connect to. No chat UI in Interlux.

## Conventions

- `[ ]` open / `[x]` proven on-device (evidence: boot log, screenshot, test)
- Boss calls the shots; this doc proposes.

## Architecture

```
Kara / other clients
        │  WebSocket JSON-RPC (versioned, capability-negotiated) + stdio (embeddable)
        ▼
┌─ Interlux agent daemon (iagent) ─────────────────────────────────────┐
│ Core language: Python 3.14 (thin, auditable)                         │
│ Transport: WS (loopback) + stdio (for embedded)                     │
│ Session: threads, turns, cancel, resume                             │
│ Stream: delta events (text/tool_call/thinking) → explicit complete  │
│ Providers: adapter registry (OpenAI, Anthropic, Google, local)      │
│ Tools: PATH executables (any lang), FS, pty-attach, plugin loaders  │
│ Approvals: Allow / Always / Deny (turn|session|policy scopes)       │
│ Audit: JSONL full transcript + tool I/O                             │
│ Config: ~/.interlux/agent/{providers,keys,policy}.json              │
└──────────────────────────────────────────────────────────────────────┘
```

## Protocol Design (Fixing Codex Pain)

| Issue | Fix |
|---|---|
| Unknown methods stall | **Versioned handshake** → client learns supported methods; unknown returns structured `unsupported`, never blocks |
| No streaming/completion | **Mandatory deltas** → every turn emits `text_delta`/`tool_call_delta` → exactly one `complete` or `error` |
| Provider-locked | **Adapter registry** → request carries `provider` + `model` + `base_url`; engine normalizes tool-calls |
| Approvals buried | **First-class requests** → same 3 labels Kara uses; deny = empty grant (not error) |

## Multi-Language Tooling

- **Agent code execution:** Full Interlux userland (`pkg`, compilers, emacs, git, nmap, etc.)
- **Tools:** Any executable on PATH (Python, Node, Ruby, C, shell, compiled binaries)
- **Plugins:** Dynamic registry; new tools added without daemon restart

## Implementation Phases

| Phase | Scope | Deliverable |
|---|---|---|
| **P0** | Core skeleton | Daemon, WS+stdio transport, session/stream/cancel, config, JSONL audit |
| **P1** | Providers | OpenAI-compatible + Anthropic adapters, tool loop, FS/shell tools, approval gate |
| **P2** | Full tools | PATH/plugin registry, pty-attach, pkg/file/git tools, capability handshake |
| **P3** | Kara client | Minimal WS client proving streaming + approvals end-to-end (Kara connects later) |
| **P4** | Local backend | llama.cpp/Ollama adapter; voice stays Kara-side |

## Kara Migration Path

1. **Point existing Kara at new daemon** — same JSON-RPC interface, just different host/port
2. **Upgrade Kara client** — add capability handshake, streaming delta handling (if missing)
3. **Phase out Codex dependency** — `start-gateway.sh` replaced by `iagent serve`
4. **Test end-to-end** — approvals, streaming, tool calls, audit log

## Configuration (Boss Decisions)

| Setting | Proposed Value | Rationale |
|---|---|---|
| Core runtime | Python 3.14 | Bundled, auditable, multi-language tools available |
| Launch | `iagent serve` in shell (then auto-host in app) | Fast first milestone, better UX later |
| Port | `4600` | Avoids Codex's 4500 clash |
| Provider priority | OpenAI-compatible + Anthropic | Both in P1; use existing API keys |
| Name | `iagent` / `interlux-agent` | Short, clear |

## Evidence Required

- [ ] Daemon starts, accepts WS connection
- [ ] Stream delta → complete flow proven with API response
- [ ] Approval request → response cycle completes
- [ ] Tool runs, writes to audit log
- [ ] Kara client connects, sees streaming + approvals

---

**Working agreements:** Boss calls the shots; this doc proposes. Items move only when acceptance holds on-device.
