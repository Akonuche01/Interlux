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
  (artifact extracts as app-release.apk -> rename to interlux-*.apk)
- pm install FAILS: system_server cannot read Termux app_data_file context
- /data/local/tmp is owned by `shell`, Termux cannot write there
- termux-open DOES NOT WORK: background shell cannot launch the installer
- WORKING: user installs manually from Files app -> Download folder
- Boot/crash logs land in /sdcard/Download as interlux-boot.log*.txt

## Crash history (fixed)
1. Instant crash at startup -> native lib loaded on main thread; moved to
   background thread via NativeLib.ensureLoaded() (commits 1d00fd5, ea3880a)
2. Crash when opening a terminal -> pty reader thread called EventSink
   success()/endOfStream() directly; Flutter requires @UiThread. Fixed by
   posting all sink calls through Handler(Looper.getMainLooper()).
   commit 6bb854c "Fix terminal crash: post EventSink calls to the main thread"

## Architecture
- android/app/src/main/cpp/pty.c : forkpty() + execv("/system/bin/sh")
  Uses Android's SYSTEM shell, NOT Termux. App is self-contained.
- No package userland yet (no apt/bash/coreutils) — that is the next big layer.
- Kotlin: Pty.kt, NativeLib.kt, InterluxApplication.kt, BootTracer.kt, CrashLogger.kt

## Notes
- Interlux and Code Studio are SEPARATE apps.
- gh CLI token is configured; user prefers deepseek models via agentrouter.
