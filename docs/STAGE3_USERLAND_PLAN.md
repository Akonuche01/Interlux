# Stage 3 — Expand the userland (plan)

Goal: grow from a lone busybox shell into a usable package-based userland,
without breaking the Stage 2 guarantees (bionic only, 16KB-page aligned,
works as `untrusted_app` on Android 15, offline-first APK).

## What we already have (verified)

- `busybox` (4KB bionic launcher) + `libbusybox.so.1.38.0` (876KB).
- Substring scan of the bundled `.so` confirms applets: `wget`, `tar`,
  `gzip`, `unzip` present; `curl`, `dpkg`, `proot` absent.
- That means **runtime download + extract works with zero new binaries**:
  `busybox wget <url> && busybox tar -xzf ...`.
- Phone log confirms extraction (`busybox.exec=true`) and `pty: start`
  across 4 boots with no crashes on v0.2.0+2.

## Stage 3 foundation (this change)

1. `INTERNET` + `ACCESS_NETWORK_STATE` permissions (native sockets need it).
2. Userland layout becomes Termux-like:
   - `$PREFIX` = `filesDir/userland` (binaries + lib)
   - `$HOME` = `filesDir/userland/home` (writable, survives re-extract)
   - `$TMPDIR` = `filesDir/userland/tmp`
   - `$ENV` = `$PREFIX/etc/profile` (sourced by `sh -i`)
3. `etc/profile` sets PATH (`$PREFIX:$PREFIX/bin:$HOME/.local/bin:...`),
   PS1, and a `fetch <url> <out>` helper around `busybox wget`.
4. `fetch.sh` downloads + extracts `.tar.gz` tool drops using only bundled
   applets (wget/tar/gzip/mkdir/rm).
5. `Userland.VERSION` bumped (`busybox-1.38.0-2`) so devices re-extract once.
   Extraction log now includes the applet count (`busybox --list | wc -l`).
6. `pty.c` sets PREFIX/HOME/TMPDIR/ENV and extends PATH with
   `$PREFIX/bin` and `$HOME/.local/bin`.

## Next increments (in order)

### 3a. CA certificates + TLS smoke test (committed, awaiting build test)
- Bundled Mozilla CA bundle (`curl.se/cacert.pem`, ~189KB) as
  `assets/userland/cacert.pem`, installed to
  `$PREFIX/etc/ssl/certs/ca-certificates.crt` on extract.
- `SSL_CERT_FILE`/`CURL_CA_BUNDLE`/`REQUESTS_CA_BUNDLE` exported in both
  `etc/profile` and `pty.c` so wget/curl/python verify out of the box.
- Extraction log reports `caBundleBytes`; in-app smoke test: `tls-test`
  (busybox wget of the Termux `InRelease` index) must print `TLS-OK`.

### 3b. curl (bionic) as the real downloader (committed, awaiting build test)
- Source: Termux `curl` 8.22.0 (aarch64, bionic, API 24+).
- Closure bundled (10 files, ~7.5MB): `curl`, `libcurl.so`, `libnghttp2.so`,
  `libnghttp3.so`, `libngtcp2.so`, `libngtcp2_crypto_ossl.so`, `libssh2.so`,
  `libssl.so.3`, `libcrypto.so.3`, `libz.so.1` (renamed from `libz.so.1.3.2`
  to match the `DT_NEEDED` name).
- Verified on PC via ELF parse: `curl` interp `/system/bin/linker64`, every
  file LOAD-aligned 16384 (16KB pages OK), NEEDED closure fully satisfied
  (`libc`/`libdl` come from the system).
- `fetch`/`fetch.sh` prefer `curl --cacert $CA -L`, busybox wget fallback.
  Extraction log reports `curl=<first line of curl --version>` (run with
  `LD_LIBRARY_PATH=$PREFIX`). `Userland.VERSION` → `busybox-1.38.0-4`.

### 3c. proot + minimal rootfs (proot binary committed, rootfs next)
- Source: Termux `proot` 5.1.107.93 (aarch64, bionic) — current as of the
  live `Packages` index audit; all bundled Termux builds re-verified current
  (curl 8.22.0, busybox 1.38.0-1, openssl 3.6.3, zlib 1.3.2, CA 2026.08.13).
- Closure bundled (5 files, ~300KB): `proot`, `libtalloc.so.2`,
  `libandroid-shmem.so`, `libexec/proot/loader`, `libexec/proot/loader32`.
- Verified via ELF parse: `proot` interp `/system/bin/linker64`, all LOAD
  16384, NEEDED fully satisfied (`libtalloc.so.2` stored under its exact
  DT_NEEDED name; `liblog`/`libc` from the system).
- Loaders stored flat in the APK (`libexec_proot_loader*`) and placed nested
  at extract time (`Userland.nestedAssets`) — safe on every host OS.
- Extraction log reports `proot=<version>`; in-app smoke test: `proot-test`
  (must print version + `PROOT-OK`). Still to prove on device: ptrace
  TRACEME from untrusted_app (Yama scope varies by OEM) — on EPERM the app
  keeps the bionic userland and reports, never crashes.
- 3c-ii (PROVEN on-device 2026-09-23): rootfs.sh installs Alpine minirootfs
  3.24.2 (sha256-verified), `iroot` enters the guest, real `whois example.com`
  query returned live data. Guest gotchas, all solved: PROOT_LOADER/*_32 env
  (Termux prefix baked in), PROOT_TMP_DIR (f2fs probe), PROOT_NO_SECCOMP,
  explicit `-b host:guest` binds (glue /tmp goes stale), HTTP bootstrap
  (guest busybox wget has no ssl_client until openssl is in), musl IPv6
  ENETUNREACH (hosts pinches help), Doze throttling during testing (foreground
  the app). apk itself is unusable under proot (built-in fetcher EOFs
  mid-download; db writer EACCES) → pkginstall.sh replaces it: resolves so:
  deps from local APKINDEX copies, wget-downloads, size+.PKGINFO-verifies,
  extracts flat .apk tars straight into /. Userland v8.

### 3d. Package manager (SUPERSEDED by Option A — boss's call)
- Original plan was runtime-fetch only. Decision: bundle the power set IN
  the APK (~205MB assets): node 26.4.0 + npm 11 + yarn, python 3.14
  (ensurepip present → `pip install sqlmap`), git 2.55, nmap 7.991, openssh
  10.5, vim 9.2, with full lib closure under `bin/`+`lib/`+`libexec/`,
  terminfo DB, and 280 recreated symlinks.
- Verified via ELF parse on PC: 199 ELFs, all linker64, all 16KB-aligned,
  NEEDED closure complete (only `libpanelw` needed `ncurses-ui-libs`, added).
- Installer: versioned wipe (keeps `home/` + Alpine guest), recursive asset
  copy, +x on bin/libexec, symlink recreation from manifest, per-tool
  version smoke lines in `interlux-boot.log`.
- `pkg` script is now for EXTRAS only (future): guest recipes + one-off
  fetches, not the core runtimes.

## Constraints that never change

- Bionic dynamic binaries only; never ship static glibc/musl (seccomp kills
  them via `set_robust_list`, see `docs/STAGE2_SECCOMP_FINDINGS.md`).
- Every ELF must be 16KB-aligned; check before commit.
- No `mount`/`chroot` (blocked); proot-via-ptrace is the only isolation story.
- APK size budget: keep the base APK lean; prefer runtime downloads over
  bundling. Full bootstrap zip (~20–50MB) stays a runtime fetch, not an asset.
- `flutter analyze` + GitHub Actions APK build must stay green on every push.
- Builds happen in CI (`flutter build apk --release`); installs are manual
  from the Files app; diagnostics via `interlux-boot.log` in Downloads.

## How to add a new binary (checklist)

1. Download the Termux aarch64 `.deb`, extract on a PC.
2. `readelf -d <bin>` → collect NEEDED `.so`s; copy the closure into
   `android/app/src/main/assets/userland/`.
3. Check 16KB alignment + interpreter `/system/bin/linker64`.
4. Bump `Userland.VERSION`, add files to `assets` list + `executables` set.
5. Push → CI builds → manual install → confirm new `userland: extracted`

## Long-term direction: Termux parity + Kali-mobile + AI pentest agent

Product goal: everything Termux does, plus Termux's gaps, plus Kali
NetHunter-rootless-style pentesting, plus an AI agent operator. All within
the `untrusted_app` sandbox (no root), so the honest capability line is:

- CAN do rootless: recon (dns/whois/curl), web testing (nikto/sqlmap/ffuf),
  `nmap -sT` connect-scan + version detect, ssh client, git, python/ruby
  scripting, login brute over TCP (hydra), report generation.
- CANNOT do without root/kernel: raw sockets (SYN-stealth/spoofing),
  WiFi monitor-mode/injection, HID/BadUSB, Bluetooth arsenal, tcpdump
  (needs CAP_NET_RAW), mount/chroot isolation. These stay out of scope
  until/unless a rooted companion exists; the app must say so in-UI rather
  than fail silently.

Termux gaps to beat: 16KB-page support (already handled here), curated
pentest tool drops (no hunt-the-deb), multi-session terminal UX, offline
tool cache, and first-class automation (scripts, then AI agent).

### Stage 4 — Kali-rootless toolset (whois PROVEN in guest; recipe live)
- `pentest.sh` runs inside the Alpine guest (`iroot /root/pentest.sh tools`),
  now backed by `pkginstall.sh` instead of `apk add`.
  Package names verified against the real Alpine 3.24 APKINDEX (main 5983 +
  community 22559 pkgs): nmap/nmap-scripts 7.99, python3 3.14, py3-pip, git,
  curl, bash, tmux, bind-tools, tcpdump (main); nikto 2.6, hydra 9.6, ffuf,
  john, vim, whois, aircrack-ng (community).
- Honest gaps (documented in-script): sqlmap/metasploit/gobuster/hashcat are
  NOT in Alpine 3.24 → sqlmap via `pip install sqlmap`; metasploit out of
  scope until a Kali rootfs. aircrack installs but is radio-locked rootless.
- `rootfs.sh install` auto-stages the recipe at `/root/pentest.sh`.
  Userland v8. Bionic-native side already covers node/python/git/nmap/ssh/vim
  without the guest.

### Stage 5 — Pentest UX
- Sessions/tabs, saved targets, one-tap tool launchers with safe defaults
  (connect-scan, no raw-socket flags), output save/share, report export.
- Explicit consent + target-ownership check before any scan module runs.

### Stage 6 — AI agent operator (last, on top of 3–5)
- Tool-calling loop over the shell (run cmd, read output, next cmd) with an
  audit log of every command + output; user prefers deepseek via agentrouter.
- Guardrails from day one: allowlisted targets, deny raw-socket/pivot
  primitives, offline kill-switch, every AI-issued command shown before exec
  (approve-per-command, then approve-per-session).
   line + applet/behavior smoke test from inside the Interlux shell.
