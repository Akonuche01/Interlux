# Interlux — Termux parity, then beyond

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
3. [ ] Shared-storage access (`~/storage` equivalent: READ/WRITE_EXTERNAL_STORAGE,
    targetSdk-28 legacy storage). Done-when: `ls ~/storage/Download` works.
4. [ ] bash as default login shell (bionic build + closure, keep ash fallback).
    Done-when: `echo $0` prints bash, profile + completions load.

## Phase 2 — Package parity (install anything, update everything)

5. [ ] pkginstall v2: full dependency solver already shipping (so: mapping);
    add removal tracking, upgrades (index refresh + version compare),
    `--dry-run` list. Done-when: install/remove/upgrade nmap round-trip.
6. [ ] Bionic-native index (Termux .deb metadata → install/upgrade node,
    python, git…). Done-when: one command upgrades the power set in place.
7. [ ] GPG/index signature verification (today: size+identity only).
    Done-when: tampered .apk is refused with a clear error.

## Phase 3 — Ecosystem parity (daily-driver completeness)

8. [ ] More runtimes: ruby, perl, php, Go toolchain (bionic builds, 16KB-checked).
9. [ ] sshd server flow (host keys, password auth, `sshd` on 8022) + SFTP.
10. [ ] proot-distro manager: named guests (Alpine, Debian, Kali-rootless),
    login shortcuts, guest snapshot/reset.
11. [ ] Device-API bridge (Termux:API equivalent): battery, clipboard,
    notifications, camera, TTS — behind explicit per-call permission UI.
12. [ ] Boot persistence: start sessions/service after reboot (opt-in).
13. [ ] Editor story: emacs + tmux + terminfo-complete (terminfo DB already ships).

## Phase 4 — Surpass (what Termux lacks, what's latest)

14. [ ] AI agent operator: tool-calling loop over the shell, audit log of every
    command+output, approve-per-command → approve-per-session, target allowlist,
    offline kill-switch. Model via agentrouter (deepseek), on-device SLM later.
15. [ ] Pentest suite built-in: attested targets (done), safe presets (done),
    shareable reports (done) → plus nikto/hydra/ffuf/sqlmap one-tap recipes,
    findings export (markdown/PDF), scan history per target.
16. [ ] Modern terminal UX Termux never got: tabs (done), extra keys (done),
    searchable scrollback, split panes, font/theme packs, URL tap-to-open.
17. [ ] Safety model for 2026 Android: consent ledger (every scan consented +
    logged), rootless-capability advisor ("needs raw sockets — unavailable
    without root" instead of silent failure), Play-independent update channel
    with signed deltas.
18. [ ] Performance: 16KB-first binaries (done), lazy userland modules
    (download node/python only if used), cold-start budget < 3s to prompt.

## Explicit non-goals (no root, no lies)

Raw sockets, WiFi monitor/injection, HID/BadUSB, packet capture, chroot/mount
isolation. The app must SAY SO in-UI whenever one is requested.

## Working agreements (unchanged)

- Bionic dynamic binaries only; 16KB-aligned; linker64-verified on PC.
- Every userland change bumps Userland.VERSION (wipe keeps home/).
- flutter analyze + flutter test green before every push.
- Device proof before marking [x]: boot-log line, screenshot, or test.
- Boss calls the shots; this doc proposes.
