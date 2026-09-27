# Interlux — Project State

App: Interlux (terminal for Android, Flutter + native C pty)
Long-term goal: Termux parity + Termux gaps + Kali-mobile (rootless) pentesting,
then an AI pentest agent on top. No-root sandbox: no raw sockets/monitor-mode/
HID — rootless recon/web-testing/scripting/connect-scan only. Roadmap:
docs/STAGE3_USERLAND_PLAN.md (Stage 3 foundation → 3a CA certs → 3b curl →
3c proot → 3d pkg → Stage 4 Kali toolset → Stage 5 pentest UX → Stage 6 AI agent).
Package: com.keneristudios.interlux
Repo: https://github.com/Akonuche01/Interlux  (PUBLIC, GPL-3.0, open source)
Sister app: Code Studio (VS Code-like) — https://github.com/Akonuche01/Code_studio (PRIVATE)

## Load-bearing platform facts (proven on-device 2026-09-23, SM-A057F/Android 15)
- targetSdk MUST be 28 (like Termux). Bisected 28..36: 29+ denies execv of any
  file under filesDir with silent EACCES (no avc on user builds) — kills the
  pty shell and every tool. 28 executes. compileSdk stays 36, sideload only.
- The APEX libandroid-selinux.so is invisible to untrusted_app, but Termux
  busybox 1.38.0 needs 21 of its symbols: we ship an NDK-built no-op stub
  (is_selinux_enabled()=0; getters fail closed). Same story for python, which
  needs the real libandroid-support.so (bundled, 20KB).
- Verification loop that works: local `flutter build apk --debug` (full
  Kotlin+CMake compile, caught 2 real bugs) + `adb install -r` + boot-log
  version lines + `run-as` spot checks. CI (.github/workflows/build.yml on
  push to main) stays the release path.

## Install loop (the only reliable path)
- gh run download <run-id> --repo Akonuche01/Interlux --dir /sdcard/Download
- pm install FAILS: system_server cannot read Termux app_data_file context
- /data/local/tmp is owned by `shell`, Termux cannot write there
- termux-open DOES NOT WORK: background shell cannot launch the installer
- WORKING: user installs manually from Files app -> Download folder
- Boot/crash logs land in /sdcard/Download as interlux-boot.log*.txt

## Crash history (fixed)
1. Instant crash at startup -> native lib loaded on main thread; moved to
   background thread via NativeLib.ensureLoaded()
2. Crash when opening a terminal -> pty reader thread called EventSink
   success()/endOfStream() directly; Flutter requires @UiThread. Fixed by
   posting all sink calls through Handler(Looper.getMainLooper()).

## STAGE 1: DONE — terminal core works
- android/app/src/main/cpp/pty.c : forkpty() + execv("/system/bin/sh")
  Uses Android's SYSTEM shell, NOT Termux. App is self-contained.
- Kotlin: Pty.kt, NativeLib.kt, InterluxApplication.kt, BootTracer.kt, CrashLogger.kt
- Terminal lifecycle hardened (reap child, close fd).

## STAGE 2: DONE — self-contained userland
Seccomp findings for THIS device (Android 15 / API 35, untrusted_app_27) are in
docs/STAGE2_SECCOMP_FINDINGS.md. Key conclusions:

- `set_robust_list`/`get_robust_list` are BLOCKED by seccomp -> every static
  glibc AND static musl binary dies instantly. NEVER ship static binaries.
- `openat2`, `faccessat2`, `chroot`, `mount`, `io_uring_setup`,
  `landlock_create_ruleset` also blocked. `ptrace` IS allowed (proot viable).
- DECISION: ship bionic-linked DYNAMIC binaries that use Android's own
  /system/bin/linker64. Verified working on device in a clean env: busybox +
  libbusybox.so.1.38.0 give a full POSIX shell (ash) + all coreutils applets.

SHIPPED + VERIFIED ON DEVICE (v0.2.0+2):
- android/app/src/main/assets/userland/{busybox,libbusybox.so.1.38.0} bundled.
  busybox is a 4KB bionic launcher; all applets live in libbusybox.so (876KB).
  Both are 16KB-aligned, so 16KB-page Android 15 devices are fine.
- Userland.kt extracts them (idempotent, version-stamped) to filesDir/userland.
- pty.c execs <userland>/busybox as `sh -i`, falls back to /system/bin/sh.
- Phone boot log confirms: `userland: extracted 2 files ... busybox.exec=true`,
  then `userland: cached`, `pty: start` across 4 boots, no crashes.

## STAGE 3: IN PROGRESS — expand the userland
Plan: docs/STAGE3_USERLAND_PLAN.md. Foundation committed, awaiting build test:

- INTERNET + ACCESS_NETWORK_STATE permissions (native sockets need them).
- Termux-like layout: PREFIX=userland, HOME=userland/home, TMPDIR=userland/tmp,
  ENV=userland/etc/profile (sourced by `sh -i`).
- etc/profile + fetch.sh: download/extract tool drops using ONLY bundled
  applets (wget/tar/gzip/unzip confirmed present in libbusybox.so).
- Userland.VERSION bumped to busybox-1.38.0-2 (one-shot re-extract);
  extraction log now reports applet count.
- Next, in order: (3a) CA certs + TLS smoke test [COMMITTED, awaiting build:
  Mozilla cacert.pem ~189KB bundled, installed to etc/ssl/certs/, SSL_CERT_FILE
  wired in profile + pty.c, `tls-test` helper, Userland v3], (3b) bionic curl
  [COMMITTED, awaiting build: Termux curl 8.22.0 + 9-lib closure, 16KB + linker
  verified, curl-first fetch, Userland v4], (3c) proot [BINARY COMMITTED,
  rootfs next: Termux proot 5.1.107.93 + talloc/shmem + loaders, 16KB +
  linker verified, `proot-test` helper, Userland v5], (3c-ii) Alpine guest
  [COMMITTED, awaiting device test: rootfs.sh installs minirootfs 3.24.2
  sha256-pinned to $HOME/.rootfs,   `iroot` entry, Userland v6], (Option A) power set [COMMITTED, awaiting
  build: node 26 + npm/yarn + python 3.14 + git + nmap + openssh + vim,
  199 ELFs linker64/16KB-verified, 280 symlinks, terminfo, wipe-keeping-home,
  Userland v7 = "full-tools-1", ~205MB assets], (Stage 4) guest pentest
  recipe [COMMITTED, needs device: pentest.sh names verified vs Alpine 3.24
  APKINDEX, auto-staged into guests, Userland v8 = "full-tools-2"],
  (guest tools PROVEN 2026-09-23: whois 5.6.6 installed via pkginstall.sh
  (wget+extract, apk unusable under proot) + live query OK; iroot env fixes
  shipped; Userland = "full-tools-8"),
  (3c) proot + rootfs, (3d) minimal pkg script over Termux .debs.

## State 2026-09-26 — agent E2E + full pentest suite green
- Agent modes ALL PASS on-device: R1 chat/stream, R2 responses/stream,
  R3 chat complete (stream=false), R4 local + api=responses (llama-server
  :4602 back up — was killed by a stale libc++_shared.so missing
  _ZNSt6__ndk113__hash_memory; pkg.sh "already installed" skipped the
  broken file, fixed by remove+install).
- pkginstall.sh v2.9 (Alpine guest): do_resolve() = one awk pass over the
  13MB APKINDEX. Old shell resolver did `cat|grep -B60` per lookup (~1s
  each) — hydra's 20+-dep tree exceeded 20 min. Also follows plain-name
  deps (perl was silently skipped before), resolves virtual provides
  (icu-data via p: tokens), skips !conflicts / pc: / if: / path tokens,
  cmd: via providers-then-names. Same contract: deps first, self last.
- Pentest TOOLS all green (verify exit=0): hydra 9.6 runs after its 99-pkg
  dep closure installed; nikto 2.6.0 runs (perl-json + perl-xml-writer —
  NOT in Alpine's D: line for nikto); bind-tools (dig), john, tcpdump,
  ffuf, nmap 7.99 + NSE. Third "20+ minute resolution" bug class now
  tracked in docs/TERMUX_PARITY_PLAN.md item 15.
- Epic B SHIPPED (commit 3921170): sandbox modes full/workspace/read-only +
  scoped approval grants (turn/session) in wipe-proof policy.json, policy RPC,
  fs_write/fs_edit/image_generate write gates. Device proof: 8/8 E2E
  (read-only blocks pre-approval, turn-scope leaves no grant, session grant
  recorded, standing grant skips approval, revoke then ask then deny),
  workspace confinement (inside OK, absolute and .. escape blocked), plus
  modes/cancel/deny regressions green.
- Epic C SHIPPED (commit 2e00257): thread sessions - persistent per-thread
  history injected into every turn (tool output folded into assistant msgs),
  thread/resume (state | audit replay | new), thread/fork (base+history+notes
  copied, independent, parent recorded), thread/compact (model summary ->
  base, history reset, counter kept, audit marker), per-thread memories.md
  injected as system msg via memories RPC. State at
  ~/.interlux/agent/threads/*.json (atomic writes, slug-safe paths).
  Device proof 25/25 E2E + full regression green.
- Epic D SHIPPED (commit 8d5578c): markdown skills - bundled agent/skills/
  + wipe-proof ~/.interlux/agent/skills/ (user wins collisions), per-turn
  catalog + body injection (params.skills list or "all"), read-only
  skill_load tool, skills RPC (list/load/refresh), skill tool plugins via
  tools_dir + scan_plugins. Device proof 14/14 E2E (incl. user skill +
  plugin registered live) + full regression green.
- Epic E SHIPPED: MCP client - spawns stdio servers from mcp.json at boot,
  tools proxied as mcp_<server>__<tool> through the same approval/audit
  path (EXTRA_WRITE: always asks, session-grantable), isError mapped,
  restart/reconcile via mcp RPC + tools_refresh. Device proof 12/12 E2E
  against a stdio echo server (approval, standing grant, error, restart).
- Epic F SHIPPED: web_search tool (Tavily-compatible, config-driven,
  read-only so no approval) + token usage accounting (normalized
  prompt/completion/total from OpenAI stream chunks, Anthropic deltas,
  Responses completed, and full bodies; surfaced as live broadcast, turn
  result field, and audit record; never in history). Device proof 6/6 E2E
  (stub roundtrip, free-tier usage captured streamed, history clean) +
  regression (llama accepts stream_options, responses fallback, cancel,
  deny). Noted: TokenHarbor 404s the daemon-default gpt-4o (unknown model
  on that gateway); explicit deepseek model required.
- Epic G SHIPPED: plan mode (turn mode plan blocks writes pre-approval in
  any sandbox; mode recorded; modes advertised) + review recipe as an
  executable read-only tool (git_diff -> model critique -> structured
  findings) + bundled review skill. Shared config extracted to
  agent/pconfig.py. Fixed userland git's fatal Termux-baked system
  gitconfig path (disabled per-subprocess). Device proof 10/10 E2E (plan
  block, exec flow intact, real diff + real free-tier critique, skill).
- Epic I.1 SHIPPED: thread/list (newest-first summaries with preview for
  history drawers) + thread/read (pure-read tail, never creates state).
  Device proof 9/9 E2E.
- Epic I.2 SHIPPED: item-level event vocabulary (thread/started,
  turn/started, item/started+completed with outcome status, enriched
  complete,   turn/completed incl. cancelled, fs/changed, skills/changed,
  mcpServer started/stopped). Device proof 9/9 E2E (ordering, ids,
  enrichment, cancel path).
- Epic I.3 SHIPPED: providers RPC (masked get, atomic set/delete, keyless
  audit, env-pin refusal), turn/steer (cancel + record + optional carrying
  turn), client-registered tools (tool/call callback over the owner socket,
  ask-first approval, timeouts/dead owners fail loudly + auto-unregister,
  owner-only unregister). Device proof 14/14 E2E.
- Epic J SHIPPED: autonomous multi-step turn loop (max_rounds per turn,
  default 1 = legacy; model sees folded tool results and continues; hard
  cap 10; rounds in result + audit). Fixed materialize_state persisting
  hollow states (one-shot unknown-thread errors + clean thread/list).
  Device proof 5/5 E2E + full battery green (grants, I.1–I.3, F-reg, G).
- Epic P SHIPPED: subagents (spawn returns immediately, background turn on
  <parent>-sub-N, status/result/list/cancel RPCs, fork context, per-thread
  approvals, unique item ids across rounds, cap 8, subagent/completed
  broadcasts). Device proof 11/11 E2E (fan-out, isolation, child approval,
  cancel, broadcasts).
- Epic Q SHIPPED: MCP remote servers (streamable HTTP: JSON/SSE replies,
  session stickiness, header auth, loud 401/refused reporting; OAuth
  explicitly deferred app-side). Device proof 5/5 E2E against a local HTTP
  stub (both transports live, approval, restart) + grants/J regression.
- Kara move-in SHIPPED (read her source 2026-09-27): initialize handshake,
  thread/archive+unarchive+unsubscribe+name/set, turn/completed items
  authority, enriched approvals (tool+path), thread/compacted + error +
  diff/updated notifications, notifications get no reply. Device proof
  15/15 E2E.
- thread/import SHIPPED (sessions survive the move): bulk-load validated
  history (fail/overwrite/append) from e.g. codex thread/read output;
  imported history reaches providers like native history. Device proof
  5/5 E2E.
- Kara identity+agency RESTORED 2026-09-27 (boss: she called herself
  Qwen, then DeepSeek, then a tool-less chatbot). Root causes, all on the
  iagent side of the move: (1) daemon had NO base/identity prompt (Codex
  shipped one engine-side) and the adapter dropped her
  `developerInstructions`, so models self-identified from weights;
  (2) nothing ever told the model the tools or the ```json convention
  exist, so no tool call ever fired; (3) max_rounds defaulted 1, so tool
  results could never return to the model; (4) OpenAI-compatible
  providers only send messages[-1] — system roles never reach the wire.
  Fix: thread/resume persists developer_instructions; every turn
  prepends persona + generated tool catalog (preamble_text, inside the
  user message); adapter forwards persona, pins thread model, sends
  max_rounds=8 (17/17 adapter tests); web_search gained a keyless
  DuckDuckGo fallback (no backend configured on device; Tavily-shape
  config still wins). Live device proof: "who are you" → "I'm Kara";
  bitcoin-price turn → web_search executed → round 2 answered $84k.
  Commits 2e75f02/6c462df/a93039d/1d97bee/b0cb7b5/b7a2504; both APKs
  rebuilt+installed. Follow-up: executed call blocks scrubbed from
  recorded history/fold (joined-text scrub — live SSE chunks fragment
  the block so per-entry regex never matched; proven live: history
  clean). Live-stream echo of the call block remains by design (can't
  unsend deltas; her activity cards already render tool runs). Daemon
  runs as userland/bin/python3 -m agent -p 4600 (no proot); stale
  generations killed by `-m agent` cmdline match.
- Kara adapter BUILT in her repo (Desktop/kara, local git): EngineClient
  interface, IagentClient translator (URL-switched, both engines alive),
  config repoints to Interlux paths. Her 500 tests pass; 11 new adapter
  tests pass; LIVE test (real socket → device daemon: handshake, thread
  with tool registration, turn, timeline decode, list) passes in 5s.
  Daemon side: policy default_provider/default_model (device set to
  tokenharbor/deepseek-v4.1-flash:free) so keyless turns work.
- Kara bots MOVED (2026-09-27): forexmind+marketmind staged to guest
  /root/bots (shebangs fixed, deps ccxt/numpy/requests, both byte-compile;
  forex runs a FULL live cycle: fundamentals, 7 pairs scored). marketmind
  needs her agent to restore analytics/patterns.py (missing upstream file,
  caller technical.py:216). Supervision: BotSupervisor + BotWatchdog +
  control actions in Interlux (gradle-verified); her Termux bots alarm
  retired. Cutover needs both APKs installed (same release key!).
- Kara move part 2 (in her repo): config/read+batchWrite mapped to
  policy/providers (16 adapter tests green), skills/list mapped, full
  505-test suite green. Her bots are Python (forexmind/marketmind) —
  runtime ready in guest/userland; sources + watchdog repointing stay
  her-agent moves. Daemon default model fixed (was gpt-4o → gateway 404).
- command/exec SHIPPED (Kara startup needs it): out-of-band shell, no
  turn/approval/quota, audited with argv redacted past argv[0], 60s cap.
  Device proof: echo/node/bash-attach shapes green. Kara paths switched
  (bash+node under userland; env-file backup skipped on iagent).
- Kara wire alignment SHIPPED (read her protocol lib): item/tool/call
  carries full DynamicToolCallParams (tool+arguments+callId+threadId+
  turnId); approve() speaks Kara/Codex literals incl. acceptForSession→
  session grants and amendment degrade; approval answers routable as
  method-less frames; turn_id in approval requests. Device proof 16/16.
- Restart surface CODE SHIPPED (Kotlin compile + manifest merge green):
  exported AgentControl service (START/STOP/RESTART/STATUS + result
  PendingIntent) under signature-level CONTROL_AGENT permission; device
  proof waits for next APK install (CI build → Files app, then adb-driven
  intents + version check here).
- Honesty audit 2026-09-26 (boss: par = everything Termux runs): NO apt/
  dpkg/pkg in bin/ (user typed apt -> not found); userland has node+python
  only; guest has python3.14+pip and perl only; ruby/php/go/java/rust/gcc
  absent everywhere (item 8's runtime claim corrected in-plan; gap tracked
  as new item 19: apt verb shim + language breadth).
- Needle 3 PROVEN on-device: pip cactus-needle 3.0.5 installs+imports in
  guest, but JAX runner + musl engine wheel absent — the working path is
  the android-arm64 native binary (~/needle, 1.2MB) + needle3.cact (35MB):
  set_light(room=kitchen) correct, 301/124 tok/s, 75MB peak RAM.
- Gap-closing 2026-09-26: `model/list` RPC (config override > live
  /models > curated; gateway returned 62 live models, no secret leaks) +
  `bin/apt` shim over pkg.sh (full lifecycle proven: update/search/show/
  install/remove tree) + languages closed out (ruby 4.0.6, PHP 8.5.1, go
  1.27.1, openjdk-17 javac+run, rust 1.98.1 native hello via
  --sysroot/-L recipe; profile exports Go env + java PATH in Userland.kt;
  GNU ar fixed via binutils). Open threads: RUSTFLAGS env ignored by this
  rustc build (use command-line flags or cargo config).
- Epic L SHIPPED (pentest depth): sqlmap 1.10.9, sherlock 0.16.2, maigret
  0.6.6, holehe, h8mail, theHarvester 5.0.0 (git source + playwright stub +
  `-u` + entry_point recipe), pwntools (ELF/pack proven; unicorn 2.1.4 kept,
  pinned build unbuildable) — all run in guest. recon.sh one-tap flow
  (nmap --unprivileged -sT + crtsh → FINDINGS.md + .scan-history.jsonl)
  proven on scanme.nmap.org (22/80 open). osint skill ships the recipes.
  Open: PDF export.

## Notes
- Interlux and Code Studio are SEPARATE apps.
- gh CLI token is configured; user prefers deepseek models via agentrouter.
- Local probe sources live in ../binaries/ ; reproducible one is in
  native/seccomp-probe/probe8.c (clang -O2 -o probe8 probe8.c && ./probe8).
