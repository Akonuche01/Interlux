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

## Notes
- Interlux and Code Studio are SEPARATE apps.
- gh CLI token is configured; user prefers deepseek models via agentrouter.
- Local probe sources live in ../binaries/ ; reproducible one is in
  native/seccomp-probe/probe8.c (clang -O2 -o probe8 probe8.c && ./probe8).
