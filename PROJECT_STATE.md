# Interlux — Project State

App: Interlux (terminal for Android, Flutter + native C pty)
Package: com.keneristudios.interlux
Repo: https://github.com/Akonuche01/Interlux  (PUBLIC, GPL-3.0, open source)
Sister app: Code Studio (VS Code-like) — https://github.com/Akonuche01/Code_studio (PRIVATE)

## Build pipeline (GitHub Actions, not local)
- .github/workflows/build.yml builds release APK on push to main
- Cannot build locally: no Android SDK/NDK/Flutter in Termux
- Local Android is arm64; hosted runner is x86_64

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

## STAGE 2: IN PROGRESS — self-contained userland
Seccomp findings for THIS device (Android 15 / API 35, untrusted_app_27) are in
docs/STAGE2_SECCOMP_FINDINGS.md. Key conclusions:

- `set_robust_list`/`get_robust_list` are BLOCKED by seccomp -> every static
  glibc AND static musl binary dies instantly. NEVER ship static binaries.
- `openat2`, `faccessat2`, `chroot`, `mount`, `io_uring_setup`,
  `landlock_create_ruleset` also blocked. `ptrace` IS allowed (proot viable).
- DECISION: ship bionic-linked DYNAMIC binaries that use Android's own
  /system/bin/linker64. Verified working on device in a clean env: busybox +
  libbusybox.so.1.38.0 give a full POSIX shell (ash) + all coreutils applets.

Next: bundle busybox as a Flutter asset, extract to app data at first run, and
switch pty.c from /system/bin/sh to the bundled busybox ash with LD_LIBRARY_PATH.

## Notes
- Interlux and Code Studio are SEPARATE apps.
- gh CLI token is configured; user prefers deepseek models via agentrouter.
- Local probe sources live in ../binaries/ ; reproducible one is in
  native/seccomp-probe/probe8.c (clang -O2 -o probe8 probe8.c && ./probe8).
