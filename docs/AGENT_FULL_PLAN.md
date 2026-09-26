# Interlux Agent — Full Capability Plan (Codex parity, then beyond)

> Positioning unchanged: Interlux stays a pure terminal; the agent is a
> capability layer (daemon + versioned protocol) any client (Kara, scripts,
> future UI) can drive. Boss calls the shots; acceptance is device proof.

## 1. What Codex can do (inventory from live Kara/Codex artifacts)

| # | Codex capability | iagent today | Verdict |
|---|---|---|---|
| 1 | Threads with streaming turns | turns + deltas + complete | PAR |
| 2 | `exec` shell commands | shell/exec/pty_run | PAR |
| 3 | File read | fs_read | PAR |
| 4 | File surgically edit (apply_patch) | whole-file fs_write only | **GAP** |
| 5 | File search (grep/glob) | none | **GAP** |
| 6 | Per-command approval (allow/deny) | per-call approve/deny | PAR |
| 7 | Approval scopes (always/session) | per-call only | **GAP** |
| 8 | Sandbox modes (read-only/workspace/full) | none | **GAP** |
| 9 | Sessions resume/fork/compact | thread ids only | **GAP** |
| 10 | Skills (markdown + tools) | plugins (code-only) | **GAP** |
| 11 | MCP servers (external tools) | none | **GAP** |
| 12 | Web search | none | **GAP** |
| 13 | Token usage + cost per turn | ignored | **GAP** |
| 14 | Plan mode (read-only loop) | none | **GAP** |
| 15 | Review flows (diff review) | git_diff exists, no flow | **GAP** |
| 16 | Streaming deltas | true SSE | PAR |
| 17 | Multi-provider + local | 5 providers + llama | AHEAD |
| 18 | Live tab attach | tabs tool + bridge | AHEAD |
| 19 | Full-disk audit trail | JSONL everything | AHEAD |
| 20 | Kill-switch cancel that kills | track + kill_turn | AHEAD |

## 2. Build order (each ships only with device proof)

### Epic A — File surgery (this round)
- `fs_edit(path, old, new)`: exact-match replace, unique-match enforced
  (mirrors the discipline that keeps our own codebase safe). Approval-gated.
- `fs_search(path, pattern)`: regex content search, capped results.
- `fs_glob(pattern)`: path pattern listing.
- Why before all else: every higher capability (skills, reviews, patch flows)
  stands on precise file ops.

### Epic B — Approval scopes + sandbox
- `approve` gains scope: `turn` (once) | `session` (always-allow this tool /
  this exact command for the thread). Stored in policy file, survives turns.
- Sandbox modes per turn: `read-only` (write tools hidden from tool list),
  `workspace` (fs_write confined under a root), `full` (today's behavior).
- `policy.json` on disk: default mode + standing grants.

### Epic C — Sessions with memory
- `thread/resume` (replay audit tail into context), `thread/fork`,
  `thread/compact` (model summarizes transcript → new base + audit marker).
- `memories.md` per thread: client-writable notes the daemon injects.

### Epic D — Skills (markdown, Codex-shaped)
- `skills/*.md` with front-matter (name, description, tools). Daemon injects
  matching skills into the system prompt; skill tools register like plugins.
- This is also the documented home for OUR future pentest tools (recipes
  graduate from shell scripts into skills with Codex-compatible shape).

### Epic E — MCP client
- Daemon spawns stdio MCP servers from config, exposes their tools through
  the same registry/approval/audit path as native tools. Kara's `kene-mcp`
  becomes portable instead of Termux-locked.

### Epic F — Web search + usage accounting
- `web_search` tool behind a configured endpoint (TokenHarbor-compatible or
  standalone key; never hardcoded). Read-only, no approval by default.
- Capture `usage` from every provider response into the audit + turn result
  (cost visibility Kara's dashboard has and we lack).

### Epic G — Plan mode + review flow
- Turn-level `mode: "plan"` (write tools hidden, like sandbox read-only but
  explicit). `review` recipe: git_diff → model critique → structured findings.

### Epic H — Multimodal input (done)
- Turn params accept `images` (file paths or `data:` URLs); per-provider
  content blocks (OpenAI + Anthropic shapes); `media: ["image"]` advertised
  in capabilities. Proven on-device: phone screenshot → free-tier vision
  model answered correctly through the full turn flow.
- `image_generate` tool: OpenAI-compatible Images API → file, approval-gated
  with model + prompt in the label, viewable roundtrip via image turns.
  Blocked on billing (402) until wallet top-up; plumbing + errors proven.

## 3. Where Codex falls short (Interlux upgrades, not parity)

1. **On-device everything**: loopback daemon + local SLM. No cloud round-trip,
   works offline, private by construction. (Proven: llama-server + Qwen 0.5B.)
2. **Live tab attach**: operate the user's real shells, not a sandbox copy.
3. **Approval UX for thumbs**: big Allow/Always/Deny cards (Kara-side UI).
4. **Provider freedom**: multi-gateway + free tiers, per-turn switching.
5. **Audit-everything + consent ledger**: every command, approval, and scan
   target logged; scans need attested targets (already in app).
6. **Rootless honesty**: root-only ops refused loudly with the reason, never
   silent failure (plan item 17).

## 4. Protocol additions (all under `protocol: 1`, additive only)

| Method | Purpose | Epic |
|---|---|---|
| `tools_refresh` | hot-load plugins (shipped) | — |
| `thread/resume`, `thread/fork`, `thread/compact` | session memory | C |
| `approve` + `scope` param | turn/session grants | B |
| `policy` | get/set sandbox + grants | B |
| `skills` | list loaded skills | D |

Turn params gain `mode` (default `exec`, or `plan`) and `sandbox`
(`full` default). Tool results gain `usage` where providers report it.

## 5. Non-goals (unchanged)

No auto-open URLs (confirm always), no raw sockets/monitor/HID, no silent
exfiltration. Deny is always available and always honored.
