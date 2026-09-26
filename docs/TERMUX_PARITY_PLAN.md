# Interlux — Termux parity, then beyond

## Scoreboard (2026-09-24, device-proven only)

| Phase | Done | Score |
|---|---|---|
| 1 — Survival (service, battery, storage, bash) | 4/4 | 100% |
| 2 — Packages (solver, index, signatures) | 3/3 | 100% |
| 3 — Ecosystem (runtimes, sshd, distros, APIs, boot, editors) | 6/6 | 100% |
| 4 — Surpass (agent, suite, UX, safety, perf) | 1.5/5 | ~30% |
| Terminal UX bonus track (tabs, keys, targets, reports) | 4/4 | 100% |
| **Overall toward strict parity** | | **~78%** |

Remaining parity gaps: X11/GUI, Perl-module/Ruby-gem depth, Termux:API
shell-CLI bindings (bridge itself done).
Honesty note 2026-09-26: Phase 2's 3/3 covers solver/index/signatures as
specified — but `apt` UX parity and language breadth were never in those
three items and are genuinely behind (see item 19). Par means everything
Termux runs; by that bar packages+languages are BEHIND, not par.
Ahead of Termux already: Kali-rootless guest + manager, attested targets
with consent ledger, shareable reports, AI-agent scaffolding (API + audit
shape), 16KB-first binaries, sshd manager.

Goal, in order: (1) do everything Termux does, (2) fix what Termux lacks,
(3) build what is latest (AI-operated, pentest-ready terminal).
This doc is the checklist. Items move only when their acceptance holds
on-device (SM-A057F class hardware, untrusted_app, no root).

Conventions: `[ ]` open, `[x]` proven on-device with evidence.
Evidence = boot-log line, screenshot, or `flutter test` — never "should work".

## Where we stand (proven)

- [x] pty shell that survives (forkpty + native bridge, targetSdk 28 exec gate)
- [x] bionic userland: busybox (276 applets), curl, proot, CA bundle
- [x] Option A power set: node 26 + npm/yarn, python 3.14, git, nmap,
      openssh, vim — all version-proven in boot log
- [x] Alpine guest under proot + pkginstall.sh (whois installed + live query)
- [x] tabs, extra-keys bar, attested targets, safe scan presets, share reports
- [x] local build + analyze + tests + ADB install + screenshot loop

## Phase 1 — Survival parity (app must not die, must reach files)

- [x] Foreground service + persistent notification (`specialUse`), START_STICKY.
    Proven 2026-09-23: same PID + live sh child after 3 min backgrounded,
    service foreground, terminal interactive on return.
- [x] Battery-optimizations prompt (Doze throttled our UID mid-test).
    Proven 2026-09-23: one-time dialog → system exemption screen → granted.
    Shown once (dismissal remembered); `interlux/power` channel.
- [x] Shared-storage access (`~/storage` equivalent).
    Proven 2026-09-23: permission granted (USER_SET), `~/storage -> /sdcard`
    symlink from login profile, Download listing flows. targetSdk 28 legacy
    storage; `interlux/storage` channel; Userland full-tools-9.
- [x] bash as default login shell (bionic build + closure, keep ash fallback).
    Proven 2026-09-23: Termux bash 5.3.20 + 43 loadable builtins
    (linker64/16KB/NEEDED verified), pty execs `bash -i` (BASH_ENV profile,
    PS1 confirmed), busybox ash + system sh fallbacks intact.

## Phase 2 — Package parity (install anything, update everything)

5. [x] pkginstall v2: full lifecycle (install/remove/upgrade/list/dry-run).
    Proven 2026-09-23 on-device: whois install (+dep closure) → remove
    (binary gone, shared libs + /etc kept via rmdir-only rule — an rm -rf
    version wiped guest /etc first, caught and fixed) → reinstall → upgrade
    no-op → versioned DB. so: resolution, size+.PKGINFO checks, manifests.
6. [x] Bionic-native index (Termux .deb metadata → install/upgrade node,
    python, git…). Proven 2026-09-23 on-device: `pkg.sh` resolves closures,
    skips current, downloads (URL-encoded), extracts via ar+tar; tmux 3.7c
    installed+ran, removed cleanly (shared libs kept), live `upgrade`
    moved proot .93→.94 + libc++ 29→30. Fixes found by testing: ^Package:
    stanza field, full-key field() strip, strip-components 6, version bump
    required for script refreshes. Note: upgrade pulled real
    libandroid-selinux into lib/ (stub at root still wins LD order; tidy later).
7. [x] Index signature verification (TLS index + per-file hashes).
    Proven 2026-09-23 on-device: bionic `pkg.sh` verifies index SHA256 per
    .deb; a deliberately corrupted tree .deb was detected, re-downloaded
    fresh, and only verified bytes installed (`tree v2.3.2` runs). Threat
    model: TLS-verified index is the trust anchor; persistent bad bits get
    a loud SHA256 MISMATCH refusal. Guest side: size + .PKGINFO identity.
    Finding: Alpine APKINDEX c:/C: fields do NOT match whole-file hashes
    (proven across dl-cdn + kernel.org mirrors serving identical bytes),
    so they can't anchor file verification — full .SIGN checking would
    need guest openssl and stays a future item.
    Done-when: tampered .apk is refused with a clear error.

## Phase 3 — Ecosystem parity (daily-driver completeness)

8. [x] More runtimes: ruby, perl, php, Go toolchain (bionic builds, 16KB-checked).
    Proven 2026-09-23/24 on-device, all via pkg.sh (zero APK bloat):
    ruby 4.0.6, perl v5.42.2, PHP 8.5.1, Go 1.27.1 android/arm64
    (+clang 21/llvm/ndk-sysroot closure). Crown proof: compiled + ran a
    Go hello-world ON the phone. Next runtime candidates (unverified):
    rust (125MB+clang), openjdk.
    CORRECTION 2026-09-26 (device re-audit: userland/bin has node+python
    only; guest has python3.14+pip and perl only; no ruby/php/go/java/
    rust anywhere): the runtime set present TODAY is python + node +
    perl(guest). Ruby/PHP/Go are not installed — moved to item 19.
9. [x] sshd server flow (host keys, password auth, `sshd` on 8022) + SFTP.
    Proven 2026-09-24 on-device: guest Alpine openssh (key auth, root),
    live exec + SFTP file get, managed by `ssh-host.sh start|stop|status`.
    Key finding: bionic sshd can NEVER work — bionic NSS synthesizes users
    and ignores /etc/passwd files (proven via getpwnam probes), so no
    account can resolve. Guest musl has a real passwd DB. Bionic client
    works (`-S` flag needed: Termux-baked ssh path). Base `sshd_config`
    overridable knobs used: SshdSessionPath/SshdAuthPath/ModuliFile.
10. [x] proot-distro manager: named guests (Alpine, Debian, Kali-rootless),
    login shortcuts, guest snapshot/reset. Proven 2026-09-24 on-device:
    `distro.sh` full cycle (create lab → list → snapshot → remove →
    restore → remove, all clean) + named guest boots 3.24.2. Default
    ~/.rootfs untouched. Debian/Kali await proot-ready tarball URLs.
11. [x] Device-API bridge (Termux:API equivalent): battery, clipboard,
    notifications, camera, TTS — behind explicit per-call permission UI.
    Proven 2026-09-24 live on-device (boss-tested): battery, clipboard
    round-trip, notify, vibrate, TTS speak, location permission granted,
    photo capture returning a real .jpg. Shell-CLI bindings stay future.
12. [x] Boot persistence: start sessions/service after reboot (opt-in).
    Proven 2026-09-24 without rebooting: TEMP test action drove the identical
    onReceive path (BOOT_COMPLETED is shell-protected) — boot log shows
    "reboot detected" → service onCreate+started from a dead process.
    Test hook removed after proof. Semantics: service runs iff the app was
    opened at least once since boot; Stop halts until next launch.
13. [x] Editor story: emacs + tmux + terminfo-complete (terminfo DB already ships).
    Proven 2026-09-24 on-device: tmux 3.7c via pkg.sh (install+run+remove);
    emacs 31.1 via pkg.sh — maintainer postinst generates the .pdmp dump
    under LD_PRELOAD pathfix (Termux-baked prefixes rewritten at every path
    syscall onto our userland); `emacs --version` + `--batch` + file load
    all EMACS-OK. pathfix ships as libpathfix.so, exported from profile.

## Phase 4 — Surpass (what Termux lacks, what's latest)

Proven early (2026-09-24): npm 11.20.0 / npx / yarn 1.22.22 run clean
(`npm ls -g` verified). Two latent tree-wide bugs fixed for it: 78
Termux-baked shebangs rewritten at extract, and OPENSSL_CONF=/dev/null
(node fatals on the unreadable baked default at first crypto use).

14. [ ] AI agent operator: tool-calling loop over the shell, audit log of every
    command+output, approve-per-command → approve-per-session, target allowlist,
    offline kill-switch. Model via agentrouter (deepseek), on-device SLM later.
    **Status:** P1 skeleton done (daemon, WS transport, provider adapters, audit, shell tool + approvals). P2: full provider APIs.
15. [ ] Pentest suite built-in: attested targets (done), safe presets (done),
    shareable reports (done) → plus nikto/hydra/ffuf/sqlmap one-tap recipes,
    findings export (markdown/PDF), scan history per target.
    Proven 2026-09-25: nmap 7.99 + 613 NSE scripts, tmux 3.7c, whois live query.
    ALL TOOLS GREEN 2026-09-26 (pentest.sh verify exit=0, smoke runs each):
    python3 3.14.7, py3-pip, git 2.54, curl, bash, tmux, bind-tools (dig
    9.20.27), vim, whois, nikto 2.6.0 (needs perl-json + perl-xml-writer —
    Alpine's D: line only lists perl/nmap/openssl), hydra 9.6 RUNS (99-pkg
    dep closure: afpfs-ng, freerdp-libs, samba-libs, mariadb-connector-c,
    libpq, subversion-libs, mongo-c-driver, libmemcached, apr, libgcrypt,
    libssh), ffuf, john 1.9-jumbo, tcpdump. pkginstall v2.9: do_resolve is
    a single awk pass over the index (old per-lookup 13MB cat|grep took
    ~1s each; hydra's deep tree blew past 20 min).
    PENDING: OSINT phase (sherlock, maigret, holehe, h8mail, theHarvester);
    pwntools attempt with Kara's psutil/PyNaCl patches.
16. [ ] Modern terminal UX Termux never got: tabs (done), extra keys (done),
    searchable scrollback (done: find + highlight + counter + prev/next,
    unit-tested, screenshot-proven; no auto-scroll yet), split panes (REMOVED
    by request — Interlux stays a pure terminal),
    font size control (done: Aa dialog + slider + persisted),
    URL tap-to-open (done: cell-mapped tap + confirm dialog, http(s) only,
    via DeviceApi; no new deps), font/theme packs.
17. [ ] Safety model for 2026 Android: consent ledger (every scan consented +
    logged), rootless-capability advisor ("needs raw sockets — unavailable
    without root" instead of silent failure), Play-independent update channel
    with signed deltas.
18. [ ] Performance: 16KB-first binaries (done), lazy userland modules
    (download node/python only if used), cold-start budget < 3s to prompt.
19. [ ] apt-compatible UX + language breadth (par = everything Termux runs).
    No apt/dpkg/pkg in bin/ (pkg.sh + pkginstall.sh exist but speak their
    own verbs) — needs an `apt` verb shim (update/install/remove/list/
    show/upgrade/search). Languages present 2026-09-26: python + node
    (userland), python + perl (guest). Missing: ruby, php, go, java,
    rust, gcc.
    PROVEN 2026-09-26 (same audit): Needle 3 runs natively — Cactus
    android-arm64 binary (1.2MB) + needle3.cact (35MB) in
    ~/needle[/3.cact], tool call set_light(room=kitchen) correct at
    301 prefill / 124 decode tok/s, 75MB peak RAM. (pip-installed
    cactus-needle 3.0.5 works too, but its JAX runner and musl engine
    wheel are absent — the native binary is the path.)

## Explicit non-goals (no root, no lies)

Raw sockets, WiFi monitor/injection, HID/BadUSB, packet capture, chroot/mount
isolation. The app must SAY SO in-UI whenever one is requested.

## Working agreements (unchanged)

- Bionic dynamic binaries only; 16KB-aligned; linker64-verified on PC.
- Every userland change bumps Userland.VERSION (wipe keeps home/).
- flutter analyze + flutter test green before every push.
- Device proof before marking [x]: boot-log line, screenshot, or test.
- Boss calls the shots; this doc proposes.
