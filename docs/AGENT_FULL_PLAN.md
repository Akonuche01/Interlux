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
| 4 | File surgically edit (apply_patch) | fs_edit exact-match replace (A) | PAR |
| 5 | File search (grep/glob) | fs_search + fs_glob (A) | PAR |
| 6 | Per-command approval (allow/deny) | per-call approve/deny | PAR |
| 7 | Approval scopes (always/session) | turn/session grants in policy.json (B) | PAR |
| 8 | Sandbox modes (read-only/workspace/full) | per-turn + policy default, fs gates (B) | PAR |
| 9 | Sessions resume/fork/compact | thread/resume\|fork\|compact + memories (C) | PAR |
| 10 | Skills (markdown + tools) | markdown skills + catalog + skill_load + skill tool plugins (D) | PAR |
| 11 | MCP servers (external tools) | stdio + streamable-HTTP clients, header auth (E/Q) | PAR |
| 12 | Web search | web_search tool, config-driven, read-only (F) | PAR |
| 13 | Token usage + cost per turn | normalized usage in result + broadcast + audit (F) | PAR |
| 14 | Plan mode (read-only loop) | turn mode plan, pre-approval block (G) | PAR |
| 15 | Review flows (diff review) | review tool + review skill (G) | PAR |
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
| `memories` | per-thread client notes (daemon injects) | C |
| `approve` + `scope` param | turn/session grants | B |
| `policy` | get/set sandbox + grants | B |
| `skills` | list loaded skills | D |

Turn params gain `mode` (default `exec`, or `plan`) and `sandbox`
(`full` default). Tool results gain `usage` where providers report it.

## 5. Non-goals (unchanged)

No auto-open URLs (confirm always), no raw sockets/monitor/HID, no silent
exfiltration. Deny is always available and always honored.

## 6. Kara-migration surface (daemon-side remainders, from 2026-09-26 assessment)

Kara can drive iagent today for chat/turns/approvals/skills/MCP/tools, but its
drawer/timeline/config need surface we have not built yet. None of it breaks
`protocol: 1` (all additive):

| # | Need | Status | Home |
|---|---|---|---|
| M1 | `thread/list` + `thread/read` (history drawer) | SHIPPED (I.1: newest-first summaries + pure-read tail) | new Epic (I) |
| M2 | Item-granular event vocabulary (item begin/end, tool args/results) | SHIPPED (I.2: thread/turn/item lifecycle, fs/changed, skills/changed, mcpServer/*) | Epic F + new Epic (I) |
| M3 | Provider key management RPC (keys live in our private filesDir; Kara cannot write the file) | SHIPPED (I.3: providers get/set/delete, masked, audited keyless) | new Epic (I) |
| M4 | `turn/steer` (mid-turn steering) or explicit wont-do | SHIPPED (I.3: cancel + record + optional carrying turn) | new Epic (I) |
| M5 | Client-registered tools (`ask_provider` callback) or explicit wont-do | SHIPPED (I.3: tools/register, tool/call callback, owner-only unregister) | new Epic (I) |
| M6 | Inter-app restart surface (auto-start already exists via TerminalService + BootReceiver; needs APK rebuild) | TODO | app-side |

Resolved by B–E: `thread/resume|fork|compact`, `memories`, `skills` list,
MCP client (`mcp_<server>__<tool>`), per-turn providers, sandboxes, audit.

## 7. Match-everything epics (2026-09-26 directive: every Termux + Codex
## remainder; split panes stays excluded by standing request)

| Epic | Scope | Notes |
|---|---|---|
| I | Kara surface: `thread/list`+`read`, item events, keys RPC, steer, client tools | daemon-side; §6 M1–M5 |
| J | Autonomous multi-step turn loop (model↔tools, bounded) | SHIPPED (max_rounds, fold-per-round, cap 10, rounds in result/audit) |
| P | Subagents (parallel background turns on child threads) | SHIPPED (spawn/status/result/list/cancel, fork context, per-thread approvals, cap 8) |
| Q | MCP remote servers (streamable HTTP + header auth) | SHIPPED (JSON/SSE replies, session stickiness, loud failures; OAuth deferred app-side) |
| K | Terminal tails: Termux:API CLI shims, URL-tap cert, font/theme packs, scrollback auto-scroll, libselinux tidy | app-side, slower loop |
| L | Pentest depth: OSINT set, pwntools, one-tap recipes, findings export, scan history | guest + app |
| M | Package depth: Perl/Ruby tail, rust/openjdk, Debian/Kali tarballs, APKINDEX SIGN | guest + userland |
| N | Perf/distribution: lazy modules, cold-start budget, signed update channel | app-side |
| O | X11/GUI via guest VNC server + in-app viewer | largest, last |
