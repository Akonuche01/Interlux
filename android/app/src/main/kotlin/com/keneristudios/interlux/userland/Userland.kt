package com.keneristudios.interlux.userland

import android.content.Context
import java.io.File
import java.io.FileOutputStream

/**
 * Extracts the bundled bionic userland (busybox launcher + libbusybox.so) from
 * the APK assets into the app's private files dir.
 *
 * Why bionic and not a static binary: Android's app seccomp filter blocks
 * set_robust_list/get_robust_list, which both glibc and musl call during
 * startup, so every static binary dies with SIGSYS. Bionic-linked binaries use
 * Android's own /system/bin/linker64 and run fine. See
 * docs/STAGE2_SECCOMP_FINDINGS.md.
 *
 * The base userland (stage 2):
 *   busybox              4 KB multicall launcher (interpreter /system/bin/linker64)
 *   libbusybox.so.1.38.0 876 KB, holds every applet
 * Stage 3 adds: Mozilla CA bundle + Termux bionic curl 8.22.0 with its lib
 * closure (libcurl, nghttp2/3, ngtcp2, ssh2, ssl/crypto, z) + Termux proot
 * 5.1.107.93 with libtalloc/android-shmem and libexec loaders + rootfs.sh
 * (Alpine minirootfs installer) with the iroot guest entry.
 * Standalone shell mode is compiled in, so `sh` resolves ls/cat/grep/... as
 * built-in applets without needing a forest of symlinks.
 */
object Userland {

    // Bump on any userland layout change so devices re-extract exactly once.
    // v1: busybox + libbusybox.so only.
    // v2 (stage3-foundation): + etc/profile, fetch helper, tmp/home dirs.
    // v3 (3a TLS): + Mozilla CA bundle at etc/ssl/certs/ca-certificates.crt.
    // v4 (3b curl): + Termux bionic curl 8.22.0 + lib closure (curl, libcurl,
    //   nghttp2/3, ngtcp2, ssh2, ssl/crypto, z). All 16KB-aligned, verified.
    // v5 (3c proot): + Termux proot 5.1.107.93 + libtalloc + android-shmem +
    //   libexec loaders. 16KB-aligned, linker64, verified.
    // v6 (3c-ii rootfs): + rootfs.sh (Alpine minirootfs installer) + iroot.
    // v7 (Option A power set): + node 26 + npm/yarn + python 3.14 + git +
    //   nmap + openssh + vim with full lib closure under bin/lib/libexec,
    //   terminfo DB, recreated symlinks. Stale trees are wiped (home kept).
    // v8 (Stage 4 guest tools): + pentest.sh (Alpine apk recipe, names verified
    //   against the 3.24 main+community index) auto-staged into new guests.
    // v9 (selinux stub): + NDK-built libandroid-selinux.so (8-symbol no-op
    //   shim) — the APEX original is invisible to untrusted_app, without it
    //   the busybox launcher cannot link. All ELFs re-verified.
    // v10 (selinux stub full): all 21 libselinux symbols busybox references
    //   (getcon/freecon/filecon/pidcon/matchpathcon/setexeccon/…). Verified
    //   via run-as: busybox links and runs.
    // v11 (targetSdk gate proof): targetSdk pinned 28 (bisected: 29..36 deny
    //   exec of filesDir). Forces one fresh extract to log all tool versions.
    // v12 (support lib): + libandroid-support.so (Termux runtime shim python
    //   needs) + LD_LIBRARY_PATH fix for the applet counter.
    // v13 (mktemp fix): busybox mktemp requires TEMPLATE to end in XXXXXX —
    //   fetch.sh/rootfs.sh used a .tar.gz suffix. Verified via run-as.
    // v14 (guest tools proven): PROOT_LOADER/*_32 + PROOT_TMP_DIR + NO_SECCOMP
    //   in iroot (proot can't find loaders otherwise); pkginstall.sh replaces
    //   apk (its fetcher EOFs + db writer EACCES under proot) — whois 5.6.6
    //   installed + live query proven on-device.
    // v15 (shared storage): ~/storage symlink line in profile (no asset
    //   changes; refresh needed only so existing installs rewrite profile).
    // v16 (bash default): + Termux bionic bash 5.3.20 + 43 loadable builtins
    //   (linker64/16KB/NEEDED verified); pty execs bash -i first (BASH_ENV
    //   profile), busybox ash stays as fallback.
    // v17 (pkginstall v2): install/remove/upgrade/list/dry-run with version
    //   DB + file manifests (shared-file safe) replacing the install-only v1.
    // v18 (pkginstall v2.1): size-mismatch retry; guest-/tmp mapping notes.
    // v19 (pkginstall v2.2): split resolve/fetch/install — recursive fetch
    //   echoes polluted captured paths and skipped dep installs.
    // v20 (pkginstall v2.3): explicit check_size/check_identity params
    //   (nested sh functions get fresh positional params).
    // v21 (pkginstall v2.4): rebuild marker — a staging mix-up left a
    //   half-old script on test devices; forces one clean re-extract.
    // v22 (pkginstall v2.5): remove uses rmdir (empty-only) — rm -rf on
    //   manifest dir entries wiped the guest /etc on-device. Directories in
    //   manifests are now harmless.
    // v23 (pkg.sh bionic): apt-lite for the power set — resolve/install/
    //   upgrade/remove over Termux .debs with DB seed of bundled versions.
    // v24 (pkg.sh fixes): correct Debian stanza field (^Package:), empty-line
    //   filtering. Lesson re-learned: code-only script changes still need a
    //   version bump or existing installs never re-extract.
    // v25 (pkg.sh fixes II): field()/field_stdin() must strip full key names
    //   (cut -c3- only fits 1-letter keys); URL-encode +/: in .deb URLs.
    // v26 (pkg.sh fixes III): busybox tar needs space-form --strip-components
    //   and count 6 (leading ./ counts); =5 form silently extracts nothing.
    // v27 (pkg.sh signatures): SHA256 from the index verified per .deb (TLS
    //   index + hash chain); tampered mirrors refused. Threat model in plan.
    // v28 (ssh-host.sh): guest sshd manager (start/stop/status); bionic sshd
    //   unfixable (NSS wall) — documented, guest path proven with live SFTP.
    // v29 (distro.sh): named Alpine guests + snapshots; iroot takes a name.
    // v30 (shebangs): rewrite 78 Termux-baked #! lines to /system interpreters
    //   (npm/yarn/git-helpers/pydoc were dead); +x on rewritten helpers.
    // v31 (openssl cnf): OPENSSL_CONF=/dev/null — node fatals on the unreadable
    //   baked default at first crypto use (npm/npx/yarn dead).
    // v32 (fetch resilience): curl --retry + resume in pkg.sh (44MB emacs deb
    //   died at 28% on flaky mobile data).
    // v33 (pkg.sh symlinks): recreate .so/.bin links at install (extraction
    //   copies real files only; emacs died on missing libacl.so.1).
    // v34 (db prune): fresh extracts prune the bionic DB to the seed —
    //   app updates wipe pkg.sh-installed files while the DB survived.
     // v35 (pathfix): LD_PRELOAD libpathfix.so rewrites Termux-baked prefixes;
     //   pkg.sh runs maintainer scripts (emacs pdump generation).
     // v36 (pkg maintainer fix): run_maintainer's $BB was mis-escaped in the
     //   Kotlin raw string ({'$'}BB literal) — postinst never ran.
     // v37 (agent): daemon bundled in APK; agent-install.sh deps + launch script.
     // v38 (tabs tool): + agent/tools/tabs.py (live-tab attach).
     // v39 (deps): + site-packages/websockets (pure py3-none-any) — the v32
     //   wipe proved third-party deps must ship in the APK, or the daemon
     //   cannot import after any re-extract.
     // v40 (pentest recipes): pentest.sh tools subcommand fix + verify/proof
     //   commands + native-tool extension point; pkginstall v2.6 provider()
     //   fixed-string match + v2.7 once-per-run index cache.
     private const val VERSION = "full-tools-34"
    private const val ASSET_DIR = "userland"
    private const val DIR_NAME = "userland"

    private val assets = listOf(
        "busybox",
        "libbusybox.so.1.38.0",
        "libandroid-selinux.so",
        "libandroid-support.so",
        "cacert.pem",
        "curl",
        "libcurl.so",
        "libnghttp2.so",
        "libnghttp3.so",
        "libngtcp2.so",
        "libngtcp2_crypto_ossl.so",
        "libssh2.so",
        "libssl.so.3",
        "libcrypto.so.3",
        "libz.so.1",
        "proot",
        "libtalloc.so.2",
        "libandroid-shmem.so",
        "libpathfix.so",
        "libexec/proot/loader",
        "libexec/proot/loader32",
    )

    /** Files that must be executable (asset-relative paths). */
    private val executables = setOf(
        "busybox", "curl", "proot",
        "libexec/proot/loader", "libexec/proot/loader32",
    )

    /**
     * Idempotent: returns the extracted userland dir, extracting (or
     * re-extracting after an app update) only when the version marker is stale.
     * The outcome is mirrored to the public Downloads log so it can be
     * diagnosed from outside the app.
     */
    fun ensure(context: Context): File {
        val dir = File(context.filesDir, DIR_NAME)
        if (!dir.exists()) dir.mkdirs()

        val marker = File(dir, ".version")
        if (marker.exists() && marker.readText().trim() == VERSION) {
            com.keneristudios.interlux.BootTracer
                .stepPublic("userland: cached at ${dir.absolutePath}")
            return dir
        }

        try {
            // Fresh layout: wipe everything except the user's home (which may
            // hold a downloaded Alpine guest worth keeping across updates).
            wipeExceptHome(dir)
            dir.mkdirs()
            for (name in assets) {
                val out = File(dir, name)
                out.parentFile?.mkdirs()
                copyAsset(context, "$ASSET_DIR/$name", out)
                out.setReadable(true, false)
                if (name in executables) {
                    out.setExecutable(true, false)
                }
            }
            for (tree in assetTrees) {
                copyAssetTree(context, "$ASSET_DIR/$tree", File(dir, tree))
            }
            fixExecBits(dir)
            val shebangs = fixShebangs(dir)
            val links = createSymlinks(context, dir)
            marker.writeText(VERSION)
            pruneBionicDb(dir)
            com.keneristudios.interlux.BootTracer.stepPublic(
                "userland: assets copied, symlinks=$links shebangs=$shebangs"
            )
        } catch (e: Exception) {
            com.keneristudios.interlux.BootTracer.stepPublic(
                "userland: FAILED ${e.javaClass.simpleName}: ${e.message}"
            )
            throw e
        }

        val busybox = File(dir, "busybox")
        // Stage 3 foundation: POSIX home layout + login profile + fetch helper.
        // busybox already ships wget/tar/gzip/unzip applets, so extra tools are
        // downloaded at runtime instead of bloating the APK. See
        // docs/STAGE3_USERLAND_PLAN.md.
        installCaBundle(dir)
        writeProfile(dir)
        File(dir, "tmp").mkdirs()
        File(dir, "home").mkdirs()
        val appletCount = countApplets(busybox)
        val caSize = caFile(dir).let { if (it.exists()) it.length() else -1 }
        val curlVer = curlVersion(dir)
        val prootVer = toolVersion(dir, "proot", "--version")
        val nodeVer = toolVersion(dir, "bin/node", "--version")
        val pyVer = toolVersion(dir, "bin/python3", "--version")
        val gitVer = toolVersion(dir, "bin/git", "--version")
        val nmapVer = toolVersion(dir, "bin/nmap", "--version")
        com.keneristudios.interlux.BootTracer.stepPublic(
            "userland: extracted to ${dir.absolutePath} " +
                "busybox.exec=${busybox.canExecute()} applets=$appletCount " +
                "caBundleBytes=$caSize curl=$curlVer proot=$prootVer " +
                "node=$nodeVer python=$pyVer git=$gitVer nmap=$nmapVer"
        )
        return dir
    }

    /**
     * Asset subtrees mirrored 1:1 into the userland (bin/, lib/, libexec/,
     * share/, etc/, agent/, site-packages/). 4k+ files: node/python/git/
     * nmap/openssh/vim + closure; site-packages holds the daemon's only
     * third-party dep (websockets, pure python) so re-extracts stay bootable.
     */
    private val assetTrees = listOf(
        "bin", "lib", "libexec", "share", "etc", "agent", "site-packages",
    )

    private fun caFile(dir: File) =
        File(dir, "etc/ssl/certs/ca-certificates.crt")

    /** Install the Mozilla CA bundle (3a) where OpenSSL/curl/python expect it. */
    private fun installCaBundle(dir: File) {
        val src = File(dir, "cacert.pem")
        val dst = caFile(dir)
        dst.parentFile.mkdirs()
        if (src.exists()) {
            src.copyTo(dst, overwrite = true)
            dst.setReadable(true, false)
        }
    }

    /** Login profile sourced by `sh -i`: PATH/HOME/PREFIX/TMPDIR + fetch alias. */
    private fun writeProfile(dir: File) {
        val etc = File(dir, "etc")
        etc.mkdirs()
        File(etc, "profile").writeText(
            """
            # Interlux userland profile (stage 3 foundation).
            export HOME="${dir.absolutePath}/home"
            export PREFIX="${dir.absolutePath}"
            export TMPDIR="${dir.absolutePath}/tmp"
            export PATH="${dir.absolutePath}/bin:${dir.absolutePath}:${dir.absolutePath}/home/.local/bin:/vendor/bin:/system/xbin:/system/bin"
            export PS1='interlux:\w\$ '
            # Option A power set: runtimes live under bin/ + lib/.
            export LD_LIBRARY_PATH="${dir.absolutePath}/lib:${dir.absolutePath}"
            export PYTHONHOME="${dir.absolutePath}"
            export TERMINFO="${dir.absolutePath}/share/terminfo"
            export TERM="xterm-256color"
            # 3a TLS: Mozilla CA bundle so HTTPS (wget/curl/python) verifies.
            export SSL_CERT_FILE="${dir.absolutePath}/etc/ssl/certs/ca-certificates.crt"
            export CURL_CA_BUNDLE="${'$'}SSL_CERT_FILE"
            export REQUESTS_CA_BUNDLE="${'$'}SSL_CERT_FILE"
            export OPENSSL_CONF=/dev/null
            # pathfix: rewrite Termux-baked absolute prefixes (emacs etc.) at
            # every path syscall onto this userland. No-op for other paths.
            export INTERLUX_PREFIX="${dir.absolutePath}"
            if [ -f "${dir.absolutePath}/libpathfix.so" ]; then
              export LD_PRELOAD="${dir.absolutePath}/libpathfix.so${'$'}{LD_PRELOAD:+:${'$'}LD_PRELOAD}"
            fi
            # fetch <url> <out>: curl first (real TLS), busybox wget fallback.
            fetch() {
              if [ -x "${dir.absolutePath}/curl" ]; then
                "${dir.absolutePath}/curl" --cacert "${dir.absolutePath}/etc/ssl/certs/ca-certificates.crt" -L -o "${'$'}2" "${'$'}1"
              else
                "${dir.absolutePath}/busybox" wget -O "${'$'}2" "${'$'}1"
              fi
            }
            # tls-test: smoke-test HTTPS against the Termux package index.
            tls-test() { "${dir.absolutePath}/busybox" wget -O /dev/null https://packages.termux.dev/apt/termux-main/dists/stable/InRelease && echo TLS-OK; }
            # 3c proot: loaders live at ${'$'}PREFIX/libexec/proot. Smoke test is
            # `proot --version`; guest rootfs via rootfs.sh + iroot (3c-ii).
            export PROOT_TMP_DIR="${dir.absolutePath}/tmp"
            proot-test() { LD_LIBRARY_PATH="${dir.absolutePath}" "${dir.absolutePath}/proot" --version && echo PROOT-OK; }
            # Phase 1.3 shared storage: ~/storage -> /sdcard (needs the storage
            # permission; the prompt lives in the app, the link is harmless
            # without it and refreshes every login with no re-extract).
            if [ ! -e "${dir.absolutePath}/home/storage" ]; then "${dir.absolutePath}/busybox" ln -s /sdcard "${dir.absolutePath}/home/storage" 2>/dev/null || true; fi
            # iroot: enter the Alpine guest (needs `rootfs.sh install` first).
            # iroot: enter the Alpine guest (needs `rootfs.sh install` first).
            # PROOT_LOADER/*_32 are mandatory: proot has Termux's prefix baked
            # in and cannot find its ELF loaders otherwise. TMP_DIR avoids the
            # f2fs-probe failure; NO_SECCOMP avoids filter quirks as app.
            iroot() {
              if [ -n "${'$'}1" ]; then R="${dir.absolutePath}/home/.guests/${'$'}1"; else R="${dir.absolutePath}/home/.rootfs"; fi
              if [ ! -d "${'$'}R" ]; then echo "no such guest (run rootfs.sh install, or distro.sh create)"; return 1; fi
              shift || true
              LD_LIBRARY_PATH="${dir.absolutePath}" PROOT_TMP_DIR="${dir.absolutePath}/tmp" PROOT_LOADER="${dir.absolutePath}/libexec/proot/loader" PROOT_LOADER_32="${dir.absolutePath}/libexec/proot/loader32" PROOT_NO_SECCOMP=1 "${dir.absolutePath}/proot" -r "${'$'}R" -0 -b /dev -b /proc -b /sys -b "${dir.absolutePath}/home:/root/host" -w /root /bin/sh --login ${'$'}@
            }
            """.trimIndent() + "\n"
        )
        // Helper script for downloading + extracting .tar.gz tool drops.
        File(dir, "fetch.sh").writeText(
            """
            #!/system/bin/sh
            # usage: fetch.sh <tar.gz-url> [dest-dir]
            # Prefers bundled curl (real TLS), falls back to busybox wget/tar.
            set -e
            BB="${dir.absolutePath}/busybox"
            PREFIX="${dir.absolutePath}"
            CA="${dir.absolutePath}/etc/ssl/certs/ca-certificates.crt"
            url="${'$'}1"
            dest="${'$'}{2:-${dir.absolutePath}/home/.local}"
            tmp="${'$'}( "${'$'}BB" mktemp "${dir.absolutePath}/tmp/dl.XXXXXX" )"
            if [ -x "${'$'}PREFIX/curl" ]; then
              LD_LIBRARY_PATH="${'$'}PREFIX" "${'$'}PREFIX/curl" --cacert "${'$'}CA" -L -o "${'$'}tmp" "${'$'}url"
            else
              "${'$'}BB" wget -O "${'$'}tmp" "${'$'}url"
            fi
            "${'$'}BB" mkdir -p "${'$'}dest"
            "${'$'}BB" tar -xzf "${'$'}tmp" -C "${'$'}dest"
            "${'$'}BB" rm -f "${'$'}tmp"
            echo "extracted ${'$'}url -> ${'$'}dest"
            """.trimIndent() + "\n"
        )
        File(dir, "fetch.sh").setExecutable(true, false)
        writeRootfsScript(dir)
        writePentestScript(dir)
    }

    /**
     * 3c-ii: Alpine minirootfs installer. Downloads the pinned tarball with
     * curl (wget fallback), verifies sha256 with busybox, extracts to
     * $HOME/.rootfs. The guest is musl-based and CANNOT run natively (seccomp
     * kills static musl); it only runs *under* proot — see iroot().
     */
    private fun writeRootfsScript(dir: File) {
        File(dir, "rootfs.sh").writeText(
            """
            #!/system/bin/sh
            # usage: rootfs.sh install|status|remove
            set -e
            BB="${dir.absolutePath}/busybox"
            PREFIX="${dir.absolutePath}"
            CA="${dir.absolutePath}/etc/ssl/certs/ca-certificates.crt"
            URL="https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/aarch64/alpine-minirootfs-3.24.2-aarch64.tar.gz"
            SHA="9bf70a7f18ea44094cbb5f70c58f9af129c8214745743db0e68e5502cc2ce773"
            ROOT="${dir.absolutePath}/home/.rootfs"
            MARKER="${'$'}ROOT/.interlux-version"
            cmd="${'$'}{1:-status}"
            case "${'$'}cmd" in
              install)
                "${'$'}BB" mkdir -p "${'$'}ROOT"
                tmp="${'$'}( "${'$'}BB" mktemp "${dir.absolutePath}/tmp/rootfs.XXXXXX" )"
                if [ -x "${'$'}PREFIX/curl" ]; then
                  LD_LIBRARY_PATH="${'$'}PREFIX" "${'$'}PREFIX/curl" --cacert "${'$'}CA" -L -o "${'$'}tmp" "${'$'}URL"
                else
                  "${'$'}BB" wget -O "${'$'}tmp" "${'$'}URL"
                fi
                echo "${'$'}SHA  ${'$'}tmp" | "${'$'}BB" sha256sum -c -
                "${'$'}BB" tar -xzf "${'$'}tmp" -C "${'$'}ROOT"
                "${'$'}BB" rm -f "${'$'}tmp"
                echo "3.24.2" > "${'$'}MARKER"
                # DNS inside the guest: Android net.dns props are invisible to
                # proot, so plant public resolvers (overwritten on reinstall).
                "${'$'}BB" mkdir -p "${'$'}ROOT/etc"
                printf 'nameserver 8.8.8.8\nnameserver 1.1.1.1\n' > "${'$'}ROOT/etc/resolv.conf"
                # Stage the pentest recipe inside the guest (runs with: iroot /root/pentest.sh tools).
                if [ -f "${'$'}PREFIX/pentest.sh" ]; then "${'$'}BB" cp "${'$'}PREFIX/pentest.sh" "${'$'}ROOT/root/pentest.sh"; fi
                # Stage the wget-based package installer (apk's own fetcher and
                # db writer fail under proot — see docs/STAGE3_USERLAND_PLAN.md).
                if [ -f "${'$'}PREFIX/pkginstall.sh" ]; then "${'$'}BB" cp "${'$'}PREFIX/pkginstall.sh" "${'$'}ROOT/root/pkginstall.sh"; "${'$'}BB" chmod +x "${'$'}ROOT/root/pkginstall.sh"; fi
                echo "guest ready at ${'$'}ROOT — enter with: iroot"
                ;;
              status)
                if [ -f "${'$'}MARKER" ]; then echo "guest ${'$'}(cat "${'$'}MARKER") at ${'$'}ROOT"; else echo "no guest (run: rootfs.sh install)"; fi
                ;;
              remove)
                "${'$'}BB" rm -rf "${'$'}ROOT"
                echo "guest removed"
                ;;
              *) echo "usage: rootfs.sh install|status|remove" >&2; exit 1;;
            esac
            """.trimIndent() + "\n"
        )
        File(dir, "rootfs.sh").setExecutable(true, false)
        writePkginstallScript(dir)
        writeDistroScript(dir)
    }

    /**
     * distro.sh: named-guest manager (create|list|enter|remove|snapshot|
     * restore|purge). The default ~/.rootfs guest stays untouched (back-
     * compat); named guests live under ~/.guests/<name>. Snapshots are plain
     * .tar.gz under ~/.snapshots. Only Alpine minirootfs is bundled as a
     * source today (same pinned 3.24.2 tarball as rootfs.sh); Debian/Kali
     * need proot-ready tarball URLs and plug in as new SOURCES entries.
     */
    private fun writeDistroScript(dir: File) {
        File(dir, "distro.sh").writeText(
            """
            #!/system/bin/sh
            # usage: distro.sh create|list|enter|remove|snapshot|restore <name> [file|distro]
            set -e
            BB="${dir.absolutePath}/busybox"
            PREFIX="${dir.absolutePath}"
            CA="${dir.absolutePath}/etc/ssl/certs/ca-certificates.crt"
            GUESTS="${dir.absolutePath}/home/.guests"
            SNAPS="${dir.absolutePath}/home/.snapshots"
            URL_ALPINE="https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/aarch64/alpine-minirootfs-3.24.2-aarch64.tar.gz"
            SHA_ALPINE="9bf70a7f18ea44094cbb5f70c58f9af129c8214745743db0e68e5502cc2ce773"
            cmd="${'$'}{1:-list}"; name="${'$'}{2:-}"; arg="${'$'}{3:-alpine}"
            root_of() { echo "${'$'}GUESTS/${'$'}1"; }
            case "${'$'}cmd" in
              list)
                ${'$'}BB echo "default: ${dir.absolutePath}/home/.rootfs"
                for g in "${'$'}GUESTS"/*/; do
                  [ -d "${'$'}g" ] || continue
                  ${'$'}BB echo "guest: $(${'$'}BB basename "${'$'}g")"
                done
                ;;
              create)
                [ -n "${'$'}name" ] || { echo "usage: distro.sh create <name> [alpine]" >&2; exit 1; }
                R=$(root_of "${'$'}name")
                if [ -d "${'$'}R" ]; then echo "distro: ${'$'}name exists"; exit 0; fi
                case "${'$'}arg" in alpine) URL="${'$'}URL_ALPINE"; SHA="${'$'}SHA_ALPINE";; *) echo "distro: unknown source ${'$'}arg (only: alpine)" >&2; exit 1;; esac
                ${'$'}BB mkdir -p "${'$'}R"
                tmp="${'$'}( "${'$'}BB" mktemp "${dir.absolutePath}/tmp/distro.XXXXXX" )"
                if [ -x "${'$'}PREFIX/curl" ]; then
                  LD_LIBRARY_PATH="${'$'}PREFIX" "${'$'}PREFIX/curl" --cacert "${'$'}CA" -L -o "${'$'}tmp" "${'$'}URL"
                else
                  "${'$'}BB" wget -O "${'$'}tmp" "${'$'}URL"
                fi
                echo "${'$'}SHA  ${'$'}tmp" | "${'$'}BB" sha256sum -c -
                "${'$'}BB" tar -xzf "${'$'}tmp" -C "${'$'}R"
                "${'$'}BB" rm -f "${'$'}tmp"
                echo "3.24.2" > "${'$'}R/.interlux-version"
                "${'$'}BB" mkdir -p "${'$'}R/etc"
                printf 'nameserver 8.8.8.8\nnameserver 1.1.1.1\n' > "${'$'}R/etc/resolv.conf"
                if [ -f "${'$'}PREFIX/pentest.sh" ]; then "${'$'}BB" cp "${'$'}PREFIX/pentest.sh" "${'$'}R/root/pentest.sh"; fi
                if [ -f "${'$'}PREFIX/pkginstall.sh" ]; then "${'$'}BB" cp "${'$'}PREFIX/pkginstall.sh" "${'$'}R/root/pkginstall.sh"; fi
                echo "distro: guest ${'$'}name ready — enter with: iroot ${'$'}name"
                ;;
              enter)
                [ -n "${'$'}name" ] || { echo "usage: distro.sh enter <name>" >&2; exit 1; }
                R=$(root_of "${'$'}name")
                [ -d "${'$'}R" ] || { echo "distro: no such guest ${'$'}name" >&2; exit 1; }
                exec iroot "${'$'}name"
                ;;
              remove)
                [ -n "${'$'}name" ] || { echo "usage: distro.sh remove <name>" >&2; exit 1; }
                R=$(root_of "${'$'}name")
                "${'$'}BB" rm -rf "${'$'}R"
                echo "distro: removed ${'$'}name"
                ;;
              snapshot)
                [ -n "${'$'}name" ] || { echo "usage: distro.sh snapshot <name> [file]" >&2; exit 1; }
                R=$(root_of "${'$'}name")
                [ -d "${'$'}R" ] || { echo "distro: no such guest ${'$'}name" >&2; exit 1; }
                out="${'$'}{4:-${'$'}SNAPS/${'$'}name.tar.gz}"
                ${'$'}BB mkdir -p "${'$'}SNAPS"
                ${'$'}BB tar -czf "${'$'}out" -C "${'$'}GUESTS" "${'$'}name"
                echo "distro: snapshot ${'$'}name -> ${'$'}out"
                ;;
              restore)
                [ -n "${'$'}name" ] || { echo "usage: distro.sh restore <name> [file]" >&2; exit 1; }
                src="${'$'}{4:-${'$'}SNAPS/${'$'}name.tar.gz}"
                [ -f "${'$'}src" ] || { echo "distro: no snapshot ${'$'}src" >&2; exit 1; }
                "${'$'}BB" rm -rf "$(root_of "${'$'}name")"
                "${'$'}BB" tar -xzf "${'$'}src" -C "${'$'}GUESTS"
                echo "distro: restored ${'$'}name from ${'$'}src"
                ;;
              *) echo "usage: distro.sh create|list|enter|remove|snapshot|restore <name>" >&2; exit 1;;
            esac
            """.trimIndent() + "\n"
        )
        File(dir, "distro.sh").setExecutable(true, false)
    }

    /**
     * Stage 4: guest pentest recipe. Runs INSIDE the Alpine guest
     * (iroot /root/pentest.sh tools). Every name below was verified against
     * the Alpine 3.24 main+community APKINDEX on 2026-09-23:
     * main: nmap 7.99, python3 3.14, py3-pip, git, curl, bash, tmux,
     *   bind-tools, tcpdump. community: nikto 2.6, hydra 9.6, ffuf, john,
     *   vim, whois, aircrack-ng (installs; radio-locked without root).
     * NOT in Alpine: sqlmap, metasploit, gobuster, hashcat -> sqlmap comes
     * via pip (pure python); metasploit stays out of scope until a Kali
     * rootfs lands.
     *
     * Recipe format (for Interlux-native tools later): each tool gets a
     * `verify_<name>()` presence check and optionally a `proof_<name>()`
     * safe loopback exercise, both registered in VERIFY_LIST/PROOF_LIST.
     * A native tool adds its two functions + list entries — no dispatcher
     * changes needed. Proofs NEVER touch foreign targets (127.0.0.1 only);
     * anything stronger goes through the agent approval gate + consent
     * ledger (Phase 4.17), not this script.
     */
    private fun writePentestScript(dir: File) {
        File(dir, "pentest.sh").writeText(
            """
            #!/bin/sh
            # usage (inside guest): sh /root/pentest.sh tools|verify|proof|list|sqlmap
            # Rootless reality check: no raw sockets (nmap -sT only), no monitor
            # mode, no HID, no packet capture. Recon + web testing + scripting
            # work fine.
            cmd="${'$'}{1:-list}"
            TOOLS="nmap nmap-scripts python3 py3-pip git curl bash tmux bind-tools vim whois nikto hydra ffuf john tcpdump"
            have() { command -v "${'$'}1" >/dev/null 2>&1; }
            case "${'$'}cmd" in
              tools)
                set -e
                /root/pkginstall.sh install ${'$'}TOOLS
                echo "guest tools ready — next: sh /root/pentest.sh verify"
                ;;
              verify)
                fail=0
                for t in ${'$'}TOOLS; do
                  case "${'$'}t" in
                    nmap-scripts) [ -d /usr/share/nmap/scripts ] && echo "ok nmap-scripts" || { echo "MISS nmap-scripts"; fail=1; } ;;
                    py3-pip) python3 -m pip --version >/dev/null 2>&1 && echo "ok py3-pip" || { echo "MISS py3-pip"; fail=1; } ;;
                    *) if have "${'$'}t"; then echo "ok ${'$'}t"; else echo "MISS ${'$'}t"; fail=1; fi ;;
                  esac
                done
                # version lines prove the binaries actually execute
                nmap --version 2>/dev/null | head -n1
                python3 --version 2>&1
                git --version 2>&1
                nikto -Version 2>&1 | head -n1
                ffuf -V 2>&1 | head -n1
                hydra 2>&1 | head -n2 | tail -n1
                john 2>&1 | head -n2 | tail -n1
                tcpdump --version 2>&1 | head -n1
                dig -v 2>&1 | head -n1
                exit ${'$'}fail
                ;;
              proof)
                set -e
                echo "--- loopback proofs (127.0.0.1 only) ---"
                nmap -sT -F 127.0.0.1 2>&1 | tail -n5
                python3 -c "print('py-ok')"
                whois -h whois.iana.org example.com 2>&1 | head -n3
                echo "proofs done"
                ;;
              sqlmap)
                set -e
                pip install --break-system-packages sqlmap 2>/dev/null || pip install sqlmap
                sqlmap --version
                ;;
              list)
                echo "tools | verify | proof | sqlmap"
                ;;
              *) echo "usage: pentest.sh tools|list|sqlmap" >&2; exit 1;;
            esac
            """.trimIndent() + "\n"
        )
        File(dir, "pentest.sh").setExecutable(true, false)
    }

    /**
     * pkginstall.sh v2: full-lifecycle Alpine package manager for the guest.
     * Proven on-device: apk's own fetcher (mid-download EOF) and db writer
     * both fail under proot, so this resolves so: deps from local APKINDEX
     * copies, downloads with busybox wget, checks size (S:) + .PKGINFO
     * identity, extracts the flat .apk tar straight into /, and tracks
     * versions + file manifests for remove/upgrade.
     * v2.6: provider() uses fixed-string match (sonames carry regex
     * metachars; tokens sit anywhere in p: lines). v2.7: index decompressed
     * once per run (per-lookup gunzip under proot took 20+ min to resolve).
     * Run INSIDE the guest: iroot /root/pkginstall.sh install|remove|
     * upgrade|list|dry-run <pkgs...>
     */
    private fun writePkginstallScript(dir: File) {
        File(dir, "pkginstall.sh").writeText(
            """
            #!/bin/busybox sh
            set -e
            export PATH=/bin:/sbin:/usr/bin:/usr/sbin
            MIRROR=http://dl-cdn.alpinelinux.org/alpine/v3.24
            IDX_MAIN=/tmp/idx-main
            IDX_COMM=/tmp/idx-comm
            IDXTXT=/tmp/idx.txt
            DL=/tmp/pkgs
            DB=/var/lib/interlux-packages
            MDIR=/var/lib/interlux-files
            BB=/bin/busybox
            mkdir -p ${'$'}DL ${'$'}MDIR
            touch ${'$'}DB
            cmd="${'$'}{1:-list}"; shift || true
            refresh_indexes() {
              ${'$'}BB rm -f ${'$'}IDX_MAIN ${'$'}IDX_COMM ${'$'}IDXTXT
              ${'$'}BB wget -O ${'$'}IDX_MAIN ${'$'}MIRROR/main/aarch64/APKINDEX.tar.gz
              ${'$'}BB wget -O ${'$'}IDX_COMM ${'$'}MIRROR/community/aarch64/APKINDEX.tar.gz
            }
            [ -f ${'$'}IDX_MAIN ] || ${'$'}BB wget -O ${'$'}IDX_MAIN ${'$'}MIRROR/main/aarch64/APKINDEX.tar.gz
            [ -f ${'$'}IDX_COMM ] || ${'$'}BB wget -O ${'$'}IDX_COMM ${'$'}MIRROR/community/aarch64/APKINDEX.tar.gz
            # v2.7: decompress both indexes ONCE per run. Every stanza/provider
            # lookup used to re-run tar+gunzip (hundreds of times under proot
            # emulation); a 15-tool closure took 20+ minutes of pure resolve.
            if [ ! -f ${'$'}IDXTXT ]; then
              { ${'$'}BB tar -xzOf ${'$'}IDX_MAIN APKINDEX 2>/dev/null; ${'$'}BB tar -xzOf ${'$'}IDX_COMM APKINDEX 2>/dev/null; } > ${'$'}IDXTXT
            fi
            dumpidx() {
              ${'$'}BB cat ${'$'}IDXTXT
            }
            stanza() {
              dumpidx | ${'$'}BB grep -A40 "^P:${'$'}1\$" | ${'$'}BB sed -n '1,/^$/p'
            }
            field() {
              echo "${'$'}1" | ${'$'}BB grep "^${'$'}2:" | ${'$'}BB head -n1 | ${'$'}BB cut -c3-
            }
            apkfile() {
              echo "$(field "${'$'}1" P)-$(field "${'$'}1" V).apk"
            }
            provider() {
              # provides lines are space-separated tokens (p:so:libA=1 so:libB=2).
              # Fixed-string match (-F): sonames contain regex metachars
              # (libstdc++.so.6 broke grep -E) and tokens sit anywhere in the
              # line. Trailing = keeps D: dep lines (no version) from matching.
              soname=$(echo "${'$'}1" | ${'$'}BB cut -d: -f2 | ${'$'}BB cut -d= -f1)
              dumpidx | ${'$'}BB grep -B60 -F "so:${'$'}soname=" | ${'$'}BB grep '^P:' | ${'$'}BB tail -n1 | ${'$'}BB cut -c3-
            }
            db_version() {
              ${'$'}BB grep "^${'$'}1 " ${'$'}DB 2>/dev/null | ${'$'}BB head -n1 | ${'$'}BB cut -d' ' -f2 || true
            }
            db_record() {
              ${'$'}BB grep -v "^${'$'}1 " ${'$'}DB 2>/dev/null > ${'$'}DB.new || true
              echo "${'$'}1 ${'$'}2" >> ${'$'}DB.new
              ${'$'}BB mv ${'$'}DB.new ${'$'}DB
            }
            db_forget() {
              ${'$'}BB grep -v "^${'$'}1 " ${'$'}DB 2>/dev/null > ${'$'}DB.new || true
              ${'$'}BB mv ${'$'}DB.new ${'$'}DB
              ${'$'}BB rm -f ${'$'}MDIR/"${'$'}1"
            }
            owned_elsewhere() {
              # $1=file $2=except-pkg -> 0 if another installed pkg lists it
              for m in ${'$'}MDIR/*; do
                [ -f "${'$'}m" ] || continue
                [ "${'$'}m" = "${'$'}MDIR/${'$'}2" ] && continue
                if ${'$'}BB grep -qxF "${'$'}1" "${'$'}m" 2>/dev/null; then return 0; fi
              done
              return 1
            }
            do_resolve() {
              # $1=pkg -> print closure (deps first, self last), one per line
              case "${'$'}done_list" in *" ${'$'}1 "*) return 0;; esac
              done_list="${'$'}done_list${'$'}1 "
              s=$(stanza "${'$'}1")
              if [ -z "${'$'}s" ]; then echo "PKGINSTALL: unknown package ${'$'}1" >&2; return 1; fi
              deps=$(echo "${'$'}s" | ${'$'}BB grep '^D:' | ${'$'}BB tr ' ' '\n' | ${'$'}BB grep '^so:' || true)
              for d in ${'$'}deps; do
                p=$(provider "${'$'}d")
                if [ -z "${'$'}p" ]; then echo "PKGINSTALL: no provider for ${'$'}d (needed by ${'$'}1)" >&2; return 1; fi
                do_resolve "${'$'}p" || return 1
              done
              echo "${'$'}1"
            }
            do_fetch_one() {
              # $1=pkg -> download single package, print apk path
              s=$(stanza "${'$'}1")
              f=$(apkfile "${'$'}s")
              if [ ! -f "${'$'}DL/${'$'}f" ]; then
                echo "PKGINSTALL: downloading ${'$'}f" >&2
                ${'$'}BB wget -O "${'$'}DL/${'$'}f" "${'$'}MIRROR/main/aarch64/${'$'}f" || ${'$'}BB wget -O "${'$'}DL/${'$'}f" "${'$'}MIRROR/community/aarch64/${'$'}f"
              fi
              echo "${'$'}DL/${'$'}f"
            }
            do_install_file() {
              # $1=pkg $2=apkpath: verify + extract + record
              s=$(stanza "${'$'}1")
              check_size() {
                # $1=apkpath (explicit: nested functions get fresh params)
                wantsize=$(field "${'$'}s" S)
                gotsize=$(${'$'}BB wc -c < "${'$'}1" | ${'$'}BB tr -d ' ')
                [ "${'$'}wantsize" = "${'$'}gotsize" ]
              }
              check_identity() {
                # $1=apkpath $2=pkgname $3=stanza (explicit: see check_size)
                id=$(${'$'}BB tar -xzOf "${'$'}1" .PKGINFO 2>/dev/null | ${'$'}BB grep -E '^(pkgname|pkgver) =')
                echo "${'$'}id" | ${'$'}BB grep -q "pkgname = ${'$'}2\$" && echo "${'$'}id" | ${'$'}BB grep -q "pkgver = $(field "${'$'}3" V)\$"
              }
              if ! check_size "${'$'}2"; then
                # Stale or partial cache: one fresh retry before giving up.
                echo "PKGINSTALL: re-downloading ${'$'}2 (size mismatch)"
                f2=$(apkfile "${'$'}s")
                ${'$'}BB rm -f "${'$'}2"
                ${'$'}BB wget -O "${'$'}2" "${'$'}MIRROR/main/aarch64/${'$'}f2" || ${'$'}BB wget -O "${'$'}2" "${'$'}MIRROR/community/aarch64/${'$'}f2"
              fi
              if ! check_size "${'$'}2"; then echo "PKGINSTALL: SIZE MISMATCH ${'$'}2" >&2; return 1; fi
              if ! check_identity "${'$'}2" "${'$'}1" "${'$'}s"; then echo "PKGINSTALL: IDENTITY MISMATCH ${'$'}2" >&2; return 1; fi
              ident=$(${'$'}BB tar -xzOf "${'$'}2" .PKGINFO 2>/dev/null | ${'$'}BB grep -E '^(pkgname|pkgver) =' | ${'$'}BB tr '\n' ' ')
              echo "PKGINSTALL: extracting ${'$'}2 [${'$'}ident]"
              ${'$'}BB tar -xf "${'$'}2" -C / --exclude=.SIGN* --exclude=.PKGINFO
              ${'$'}BB tar -tf "${'$'}2" 2>/dev/null | ${'$'}BB grep -v -E '^(\.SIGN|\.PKGINFO)' | ${'$'}BB sed 's,^\./,,' | ${'$'}BB sort > "${'$'}MDIR/${'$'}1"
              db_record "${'$'}1" "$(field "${'$'}s" V)"
            }
            cmd_install() {
              done_list=" "
              closure=""
              for p in ${'$'}@; do
                closure="${'$'}closure $(do_resolve "${'$'}p" || return 1)"
              done
              for p in ${'$'}closure; do
                f=$(do_fetch_one "${'$'}p") || return 1
                do_install_file "${'$'}p" "${'$'}f" || return 1
              done
              echo "PKGINSTALL: installed: ${'$'}@"
            }
            cmd_remove() {
              for p in ${'$'}@; do
                if [ ! -f "${'$'}MDIR/${'$'}p" ]; then echo "PKGINSTALL: not installed: ${'$'}p"; continue; fi
                # Delete deepest paths first. Directories are only removed when
                # EMPTY (rmdir): a manifest always lists ancestor dirs (etc/,
                # usr/), and rm -rf on those would nuke other packages' and
                # system files. Files owned by other installed packages stay.
                ${'$'}BB sort -r "${'$'}MDIR/${'$'}p" | while IFS= read -r f; do
                  [ -n "${'$'}f" ] || continue
                  if owned_elsewhere "${'$'}f" "${'$'}p"; then continue; fi
                  if [ -d "/${'$'}f" ] && [ ! -L "/${'$'}f" ]; then
                    ${'$'}BB rmdir "/${'$'}f" 2>/dev/null || true
                  else
                    ${'$'}BB rm -f "/${'$'}f" 2>/dev/null || true
                  fi
                done
                db_forget "${'$'}p"
                echo "PKGINSTALL: removed: ${'$'}p"
              done
            }
            cmd_upgrade() {
              refresh_indexes
              changed=0
              for rec in $( ${'$'}BB cut -d' ' -f1 ${'$'}DB 2>/dev/null || true ); do
                [ -n "${'$'}rec" ] || continue
                s=$(stanza "${'$'}rec")
                [ -n "${'$'}s" ] || continue
                if [ "$(field "${'$'}s" V)" != "$(db_version "${'$'}rec")" ]; then
                  echo "PKGINSTALL: upgrading ${'$'}rec"
                  cmd_install "${'$'}rec" || return 1
                  changed=1
                fi
              done
              [ "${'$'}changed" = "0" ] && echo "PKGINSTALL: everything up to date"
            }
            cmd_dry_run() {
              done_list=" "
              for p in ${'$'}@; do
                closure=$(do_resolve "${'$'}p") || return 1
                for q in ${'$'}closure; do
                  echo "PKGINSTALL: would install ${'$'}q"
                done
              done
            }
            case "${'$'}cmd" in
              install) cmd_install ${'$'}@;;
              remove) cmd_remove ${'$'}@;;
              upgrade) cmd_upgrade;;
              list) ${'$'}BB cat ${'$'}DB 2>/dev/null || true;;
              dry-run) cmd_dry_run ${'$'}@;;
              *) echo "usage: pkginstall.sh install|remove|upgrade|list|dry-run <pkgs...>" >&2; exit 1;;
            esac
            """.trimIndent() + "\n"
        )
        File(dir, "pkginstall.sh").setExecutable(true, false)
        writePkgScript(dir)
        writeBionicDbSeed(dir)
        writeSshHostScript(dir)
    }

    /**
     * ssh-host.sh: manage the guest OpenSSH server (start|stop|status).
     * Why guest, not bionic sshd: bionic NSS synthesizes users and ignores
     * /etc/passwd files, so host-side sshd can never resolve an account
     * (proven on-device). The Alpine guest has a real passwd DB, so its
     * sshd + key auth + SFTP work end to end (proven: live query + file get).
     * Needs `pkginstall.sh install openssh-server openssh-sftp-server
     * openssh-keygen` first (checked, with a clear error otherwise).
     */
    private fun writeSshHostScript(dir: File) {
        File(dir, "ssh-host.sh").writeText(
            """
            #!/system/bin/sh
            set -e
            PREFIX="${dir.absolutePath}"
            BB="${dir.absolutePath}/busybox"
            R="${dir.absolutePath}/home/.rootfs"
            export LD_LIBRARY_PATH="${dir.absolutePath}:${dir.absolutePath}/lib"
            export PROOT_TMP_DIR="${dir.absolutePath}/tmp"
            export PROOT_LOADER="${dir.absolutePath}/libexec/proot/loader"
            export PROOT_LOADER_32="${dir.absolutePath}/libexec/proot/loader32"
            export PROOT_NO_SECCOMP=1
            PROOT="${dir.absolutePath}/proot"
            CONF=/etc/ssh/sshd_config.interlux
            LOG="${dir.absolutePath}/tmp/sshd.log"
            PORT=8022
            cmd="${'$'}{1:-status}"
            need_guest() {
              if [ ! -d "${'$'}R/bin" ]; then echo "ssh-host: no guest (run: rootfs.sh install)" >&2; exit 1; fi
              if [ ! -x "${'$'}R/usr/sbin/sshd" ]; then echo "ssh-host: guest openssh-server missing (run in guest: /root/pkginstall.sh install openssh-server openssh-sftp-server openssh-keygen)" >&2; exit 1; fi
            }
            guest() {
              "${'$'}PROOT" -r "${'$'}R" -0 -b /dev -b /proc -b /sys -w /root /bin/busybox sh -c "${'$'}1"
            }
            ensure_keys() {
              guest '/bin/busybox mkdir -p /etc/ssh /root/.ssh /run/sshd'
              guest '[ -f /etc/ssh/ssh_host_ed25519_key ] || /usr/bin/ssh-keygen -t ed25519 -f /etc/ssh/ssh_host_ed25519_key -N "" >/dev/null 2>&1'
              guest '[ -f /etc/ssh/ssh_host_rsa_key ] || /usr/bin/ssh-keygen -t rsa -b 3072 -f /etc/ssh/ssh_host_rsa_key -N "" >/dev/null 2>&1'
              guest '[ -f /root/.ssh/id_ed25519 ] || /usr/bin/ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N "" >/dev/null 2>&1'
              guest '/bin/busybox cp /root/.ssh/id_ed25519.pub /root/.ssh/authorized_keys; /bin/busybox chmod 600 /root/.ssh/authorized_keys'
              guest 'printf "Port 8022\nHostKey /etc/ssh/ssh_host_ed25519_key\nHostKey /etc/ssh/ssh_host_rsa_key\nPidFile /run/sshd.pid\nAuthorizedKeysFile /root/.ssh/authorized_keys\nPasswordAuthentication no\nPubkeyAuthentication yes\nStrictModes no\nPrintMotd no\nSubsystem sftp /usr/lib/ssh/sftp-server\n" > /etc/ssh/sshd_config.interlux'
            }
            port_open() {
              "${dir.absolutePath}/bin/bash" -c 'echo > /dev/tcp/127.0.0.1/8022' 2>/dev/null
            }
            case "${'$'}cmd" in
              start)
                need_guest
                ensure_keys
                if port_open; then echo "ssh-host: already listening on 8022"; exit 0; fi
                "${'$'}BB" setsid "${'$'}PROOT" -r "${'$'}R" -0 -b /dev -b /proc -b /sys -w /root /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config.interlux >>"${'$'}LOG" 2>&1 &
                sleep 3
                if port_open; then echo "ssh-host: guest sshd on 127.0.0.1:8022 (key: ~/.ssh/id_ed25519 inside guest; connect: ssh -p 8022 -i <key> root@127.0.0.1)"; else echo "ssh-host: FAILED to start (see ${'$'}LOG)" >&2; exit 1; fi
                ;;
              stop)
                "${'$'}BB" pkill -f sshd_config.interlux 2>/dev/null || true
                echo "ssh-host: stopped"
                ;;
              status)
                if port_open; then echo "ssh-host: listening on 127.0.0.1:8022"; else echo "ssh-host: down"; fi
                ;;
              *) echo "usage: ssh-host.sh start|stop|status" >&2; exit 1;;
            esac
            """.trimIndent() + "\n"
        )
        File(dir, "ssh-host.sh").setExecutable(true, false)
    }

    /** Versions of the APK-bundled power set (from the Termux index audit). */
    private val bundledPkgs = mapOf(
        "busybox" to "1.38.0-1",
        "curl" to "8.22.0",
        "libcurl" to "8.22.0",
        "libnghttp2" to "1.70.0",
        "libnghttp3" to "1.18.0",
        "libngtcp2" to "1.25.0",
        "libssh2" to "1.11.1-2",
        "openssl" to "1:3.6.3",
        "zlib" to "1.3.2",
        "proot" to "5.1.107.93",
        "libtalloc" to "2.4.3",
        "libandroid-shmem" to "0.7",
        "libandroid-selinux" to "stub-1",
        "libandroid-support" to "29-1",
        "nodejs" to "26.4.0-1",
        "npm" to "11.20.0",
        "yarn" to "1.22.22",
        "python" to "3.14.6-1",
        "git" to "2.55.0",
        "nmap" to "7.991",
        "openssh" to "10.5p1",
        "openssh-sftp-server" to "10.5p1",
        "vim" to "9.2.1100",
        "bash" to "5.3.20",
        "libc++" to "29",
        "libffi" to "3.8.0",
        "libicu" to "78.3",
        "libsqlite" to "3.53.4",
        "gdbm" to "1.26-1",
        "libandroid-posix-semaphore" to "0.1-4",
        "libbz2" to "1.0.8-8",
        "libcrypt" to "0.2-6",
        "libexpat" to "2.8.5",
        "liblzma" to "5.8.4",
        "ncurses" to "6.6.20260307+really6.5.20250830",
        "ncurses-ui-libs" to "6.6.20260307+really6.5.20250830",
        "readline" to "8.3.6",
        "zstd" to "1.5.7-1",
        "less" to "710",
        "libiconv" to "1.19",
        "pcre2" to "10.47",
        "libpcap" to "1.10.5-1",
        "lua54" to "5.4.8-10",
        "krb5" to "1.22.2",
        "ldns" to "1.8.4-1",
        "libandroid-glob" to "0.6-3",
        "libdb" to "18.1.40-6",
        "libedit" to "20260512-3.1-0",
        "libresolv-wrapper" to "1.1.7-6",
        "termux-auth" to "1.5.0-1",
        "libsodium" to "1.0.22-1",
        "c-ares" to "1.34.8",
    )

    /** Pre-seed the bionic DB so pkg.sh knows the APK-bundled set. */
    private fun writeBionicDbSeed(dir: File) {
        val db = File(dir, "var/lib/bionic/installed")
        if (db.exists()) return // upgrades survive app updates
        db.parentFile.mkdirs()
        db.writeText(bundledPkgs.entries.joinToString("\n") { "${it.key} ${it.value}" } + "\n")
    }

    /**
     * App updates wipe everything except home/ and var/ — including files
     * that pkg.sh installed on top. A DB that still lists them would lie
     * (skip-checks pass, binaries gone). So on a fresh extract, prune the DB
     * back to the APK-bundled seed and drop orphan manifests; users
     * reinstall extras with one `pkg.sh install` (files, not versions, move).
     */
    private fun pruneBionicDb(dir: File) {
        try {
            val db = File(dir, "var/lib/bionic/installed")
            val manifests = File(dir, "var/lib/bionic/files")
            val seed = bundledPkgs.keys
            if (!db.exists()) return
            val kept = db.readLines().filter { line ->
                val name = line.substringBefore(" ").trim()
                name.isEmpty() || name in seed
            }
            db.writeText(kept.joinToString("\n").trimEnd() + "\n")
            manifests.listFiles()?.forEach {
                if (it.name !in seed) {
                    try {
                        it.delete()
                    } catch (_: Throwable) {
                    }
                }
            }
        } catch (_: Throwable) {
        }
    }

    /**
     * pkg.sh: apt-lite for the bionic side. Termux Depends use package names
     * (no so: mapping needed): resolve closure from a local Packages copy,
     * download .debs, unpack data.tar.* with busybox ar+tar (5 path
     * components stripped), track versions + manifests. Refuses to remove
     * base-system packages that have no manifest.
     * Run in the shell: pkg.sh update|install|remove|upgrade|list <pkgs...>
     */
    private fun writePkgScript(dir: File) {
        File(dir, "pkg.sh").writeText(
            """
            #!/system/bin/sh
            set -e
            PREFIX="${dir.absolutePath}"
            export LD_LIBRARY_PATH="${dir.absolutePath}/lib:${dir.absolutePath}"
            BB="${dir.absolutePath}/busybox"
            CURL="${dir.absolutePath}/curl"
            CA="${dir.absolutePath}/etc/ssl/certs/ca-certificates.crt"
            ARCH=aarch64
            REPO=https://packages.termux.dev/apt/termux-main
            VDIR=${'$'}PREFIX/var/lib/bionic
            IDX=${'$'}VDIR/Packages
            DB=${'$'}VDIR/installed
            MDIR=${'$'}VDIR/files
            DL=${'$'}PREFIX/tmp/debs
            mkdir -p ${'$'}VDIR ${'$'}MDIR ${'$'}DL
            touch ${'$'}DB
            fetch_url() {
              # --retry-all-errors + -C - : mobile networks stall and reset
              # mid-download (proven: 44MB emacs deb died at 28%). Resume
              # continues partial files instead of restarting them.
              if [ -x "${'$'}CURL" ]; then
                LD_LIBRARY_PATH="${dir.absolutePath}" "${'$'}CURL" --cacert "${'$'}CA" --retry 5 --retry-all-errors -C - -L -o "${'$'}2" "${'$'}1"
              else
                "${'$'}BB" wget -O "${'$'}2" "${'$'}1"
              fi
            }
            cmd="${'$'}{1:-list}"; shift || true
            if [ "${'$'}cmd" = "update" ]; then
              fetch_url "${'$'}REPO/dists/stable/main/binary-${'$'}ARCH/Packages.gz" "${'$'}IDX.gz"
              "${'$'}BB" gunzip -f "${'$'}IDX.gz"
              echo "PKG: index refreshed"
              exit 0
            fi
            [ -f "${'$'}IDX" ] || { echo "PKG: no index, run: pkg.sh update" >&2; exit 1; }
            stanza() {
              ${'$'}BB grep -A20 "^Package: ${'$'}1\$" "${'$'}IDX" | ${'$'}BB sed -n '1,/^$/p'
            }
            field() {
              echo "${'$'}1" | ${'$'}BB grep "^${'$'}2:" | ${'$'}BB head -n1 | ${'$'}BB sed "s/^${'$'}2: //"
            }
            debfile() {
              field "${'$'}1" Filename
            }
            deppkgs() {
              # Depends: "a (>= 1), b | c" -> "a" "b" (first alternate wins)
              echo "${'$'}1" | ${'$'}BB grep '^Depends:' | ${'$'}BB cut -c9- | ${'$'}BB tr ',' '\n' | ${'$'}BB cut -d'|' -f1 | ${'$'}BB sed 's/([^)]*)//g; s/^ *//; s/ *$//' | ${'$'}BB grep -v '^$' || true
            }
            db_version() {
              ${'$'}BB grep "^${'$'}1 " ${'$'}DB 2>/dev/null | ${'$'}BB head -n1 | ${'$'}BB cut -d' ' -f2 || true
            }
            db_record() {
              ${'$'}BB grep -v "^${'$'}1 " ${'$'}DB 2>/dev/null > ${'$'}DB.new || true
              echo "${'$'}1 ${'$'}2" >> ${'$'}DB.new
              ${'$'}BB mv ${'$'}DB.new ${'$'}DB
            }
            db_forget() {
              ${'$'}BB grep -v "^${'$'}1 " ${'$'}DB 2>/dev/null > ${'$'}DB.new || true
              ${'$'}BB mv ${'$'}DB.new ${'$'}DB
              ${'$'}BB rm -f "${'$'}MDIR/${'$'}1"
            }
            owned_elsewhere() {
              for m in ${'$'}MDIR/*; do
                [ -f "${'$'}m" ] || continue
                [ "${'$'}m" = "${'$'}MDIR/${'$'}2" ] && continue
                if ${'$'}BB grep -qxF "${'$'}1" "${'$'}m" 2>/dev/null; then return 0; fi
              done
              return 1
            }
            do_resolve() {
              case "${'$'}done_list" in *" ${'$'}1 "*) return 0;; esac
              done_list="${'$'}done_list${'$'}1 "
              s=$(stanza "${'$'}1")
              if [ -z "${'$'}s" ]; then echo "PKG: unknown package ${'$'}1" >&2; return 1; fi
              for d in $(deppkgs "${'$'}s"); do
                do_resolve "${'$'}d" || return 1
              done
              echo "${'$'}1"
            }
            do_fetch_one() {
              s=$(stanza "${'$'}1")
              f=$(debfile "${'$'}s")
              b=$(${'$'}BB basename "${'$'}f")
              want=$(field "${'$'}s" SHA256)
              check_hash() {
                # $1=debpath (explicit): index SHA256 must match, when known
                [ -z "${'$'}want" ] && return 0
                got=$(${'$'}BB sha256sum "${'$'}1" | ${'$'}BB cut -d' ' -f1)
                [ "${'$'}want" = "${'$'}got" ]
              }
              if [ ! -f "${'$'}DL/${'$'}b" ] || ! check_hash "${'$'}DL/${'$'}b"; then
                if [ -f "${'$'}DL/${'$'}b" ]; then echo "PKG: hash mismatch, re-downloading ${'$'}b" >&2; fi
                echo "PKG: downloading ${'$'}b" >&2
                # .deb filenames contain + and : (version epochs) which curl
                # rejects raw; busybox wget takes them as-is.
                urlpath=$(echo "${'$'}f" | ${'$'}BB sed 's/+/%2B/g; s/:/%3A/g')
                ${'$'}BB rm -f "${'$'}DL/${'$'}b"
                fetch_url "${'$'}REPO/${'$'}urlpath" "${'$'}DL/${'$'}b" || return 1
              fi
              if ! check_hash "${'$'}DL/${'$'}b"; then echo "PKG: SHA256 MISMATCH ${'$'}b (tampered mirror?)" >&2; return 1; fi
              echo "${'$'}DL/${'$'}b"
            }
            do_install_file() {
              # $1=pkg $2=debpath: unpack data.tar.* (5 components stripped)
              s=$(stanza "${'$'}1")
              member=$(${'$'}BB ar t "${'$'}2" 2>/dev/null | ${'$'}BB grep '^data.tar' | ${'$'}BB head -n1)
              if [ -z "${'$'}member" ]; then echo "PKG: no data archive in ${'$'}2" >&2; return 1; fi
              case "${'$'}member" in
                *.xz) dec="unxz -c";;
                *.gz) dec="gunzip -c";;
                *.bz2) dec="bunzip2 -c";;
                *) dec="cat";;
              esac
              echo "PKG: installing ${'$'}1"
              ${'$'}BB ar p "${'$'}2" "${'$'}member" | ${'$'}BB ${'$'}dec | ${'$'}BB tar -x --strip-components 6 -C "${'$'}PREFIX"
              # Recreate .so/.bin symlinks (skipped at extract: only real files
              # are copied, e.g. libacl.so.1.2.3 without its libacl.so.1 link
              # that binaries actually NEEDED). Map archive paths the same way.
              ${'$'}BB ar p "${'$'}2" "${'$'}member" | ${'$'}BB ${'$'}dec | ${'$'}BB tar -tv 2>/dev/null | ${'$'}BB grep -- ' -> ' | while IFS= read -r line; do
                tgt="${'$'}{line##* -> }"
                pre="${'$'}{line%% -> *}"
                link="${'$'}{pre##* }"
                case "${'$'}link" in ./*) link="${'$'}{link#./}";; esac
                case "${'$'}link" in data/data/com.termux/files/usr/*) link="${'$'}{link#data/data/com.termux/files/usr/}";; *) continue;; esac
                if [ -n "${'$'}link" ] && [ -n "${'$'}tgt" ]; then
                  ${'$'}BB mkdir -p "${'$'}PREFIX/$(dirname "${'$'}link")"
                  ${'$'}BB ln -sf "${'$'}tgt" "${'$'}PREFIX/${'$'}link"
                fi
              done
              ${'$'}BB ar p "${'$'}2" "${'$'}member" | ${'$'}BB ${'$'}dec | ${'$'}BB tar -t 2>/dev/null | ${'$'}BB sed 's,^./data/data/com.termux/files/usr/,,' | ${'$'}BB grep -v -E '^(\./)?data/' | ${'$'}BB sort -u > "${'$'}MDIR/${'$'}1"
              db_record "${'$'}1" "$(field "${'$'}s" Version)"
              ${'$'}BB chmod +x "${'$'}PREFIX/bin/"* 2>/dev/null || true
              run_maintainer "${'$'}1" "${'$'}2"
            }
            run_maintainer() {
              # $1=pkg $2=debpath: run postinst if present (emacs needs it to
              # generate the .pdmp dump). Rewrite baked Termux paths so the
              # script operates on ${'$'}PREFIX; tolerate missing helpers.
              cm="${'$'}( ${'$'}BB ar t "${'$'}2" 2>/dev/null | ${'$'}BB grep '^control.tar' | ${'$'}BB head -n1 )"
              [ -n "${'$'}cm" ] || return 0
              case "${'$'}cm" in
                *.xz) cdec="unxz -c";;
                *.gz) cdec="gunzip -c";;
                *.bz2) cdec="bunzip2 -c";;
                *) cdec="cat";;
              esac
              ct="${'$'}( "${'$'}BB" mktemp "${'$'}PREFIX/tmp/ctrl.XXXXXX" )"
              ${'$'}BB ar p "${'$'}2" "${'$'}cm" | ${'$'}BB ${'$'}cdec > "${'$'}ct" || { ${'$'}BB rm -f "${'$'}ct"; return 0; }
              pi="${'$'}( "${'$'}BB" mktemp "${'$'}PREFIX/tmp/postinst.XXXXXX" )"
              if ${'$'}BB tar -xOf "${'$'}ct" ./postinst > "${'$'}pi" 2>/dev/null || ${'$'}BB tar -xOf "${'$'}ct" postinst > "${'$'}pi" 2>/dev/null; then
                if [ -s "${'$'}pi" ]; then
                  ${'$'}BB sed -i "s|/data/data/com.termux/files/usr|${'$'}PREFIX|g" "${'$'}pi" 2>/dev/null || true
                  echo "PKG: maintainer: ${'$'}1"
                  INTERLUX_PREFIX="${'$'}PREFIX" LD_PRELOAD="${'$'}PREFIX/libpathfix.so" \
                    "${'$'}BB" sh "${'$'}pi" configure >/dev/null 2>&1 || \
                    INTERLUX_PREFIX="${'$'}PREFIX" LD_PRELOAD="${'$'}PREFIX/libpathfix.so" \
                    "${'$'}PREFIX/bin/bash" "${'$'}pi" configure || \
                    echo "PKG: maintainer failed (non-fatal): ${'$'}1" >&2
                fi
              fi
              ${'$'}BB rm -f "${'$'}ct" "${'$'}pi"
              return 0
            }
            cmd_install() {
              done_list=" "
              closure=""
              for p in ${'$'}@; do
                closure="${'$'}closure $(do_resolve "${'$'}p" || return 1)"
              done
              for p in ${'$'}closure; do
                if [ "$(db_version "${'$'}p")" = "$(stanza "${'$'}p" | field_stdin Version)" ] && [ -n "$(db_version "${'$'}p")" ]; then
                  echo "PKG: already installed: ${'$'}p"
                  continue
                fi
                f=$(do_fetch_one "${'$'}p") || return 1
                do_install_file "${'$'}p" "${'$'}f" || return 1
              done
              echo "PKG: installed: ${'$'}@"
            }
            field_stdin() {
              ${'$'}BB grep "^${'$'}1:" | ${'$'}BB head -n1 | ${'$'}BB sed "s/^${'$'}1: //"
            }
            cmd_remove() {
              for p in ${'$'}@; do
                if [ ! -f "${'$'}MDIR/${'$'}p" ]; then echo "PKG: not installed (or base system, refusing): ${'$'}p"; continue; fi
                ${'$'}BB sort -r "${'$'}MDIR/${'$'}p" | while IFS= read -r f; do
                  [ -n "${'$'}f" ] || continue
                  if owned_elsewhere "${'$'}f" "${'$'}p"; then continue; fi
                  if [ -d "${'$'}PREFIX/${'$'}f" ] && [ ! -L "${'$'}PREFIX/${'$'}f" ]; then
                    ${'$'}BB rmdir "${'$'}PREFIX/${'$'}f" 2>/dev/null || true
                  else
                    ${'$'}BB rm -f "${'$'}PREFIX/${'$'}f" 2>/dev/null || true
                  fi
                done
                db_forget "${'$'}p"
                echo "PKG: removed: ${'$'}p"
              done
            }
            cmd_upgrade() {
              fetch_url "${'$'}REPO/dists/stable/main/binary-${'$'}ARCH/Packages.gz" "${'$'}IDX.gz"
              "${'$'}BB" gunzip -f "${'$'}IDX.gz"
              changed=0
              for rec in $(${ '$'}BB cut -d' ' -f1 ${'$'}DB 2>/dev/null || true); do
                [ -n "${'$'}rec" ] || continue
                s=$(stanza "${'$'}rec")
                [ -n "${'$'}s" ] || continue
                if [ "$(field "${'$'}s" Version)" != "$(db_version "${'$'}rec")" ]; then
                  echo "PKG: upgrading ${'$'}rec"
                  cmd_install "${'$'}rec" || return 1
                  changed=1
                fi
              done
              [ "${'$'}changed" = "0" ] && echo "PKG: everything up to date"
            }
            case "${'$'}cmd" in
              install) cmd_install ${'$'}@;;
              remove) cmd_remove ${'$'}@;;
              upgrade) cmd_upgrade;;
              list) ${'$'}BB cat ${'$'}DB 2>/dev/null || true;;
              *) echo "usage: pkg.sh update|install|remove|upgrade|list <pkgs...>" >&2; exit 1;;
            esac
            """.trimIndent() + "\n"
        )
        File(dir, "pkg.sh").setExecutable(true, false)
    }

    /** Best-effort `curl --version` first line; never throws ("unknown" if N/A). */
    private fun curlVersion(prefix: File): String =
        toolVersion(prefix, "curl", "--version")

    /**
     * Best-effort `<tool> <arg>` first line; never throws ("unknown" if N/A).
     * [relTool] is the tool path relative to the userland root (e.g. "proot"
     * or "bin/node"). The loader searches both the root (busybox-era libs)
     * and lib/ (Option A power set).
     */
    private fun toolVersion(prefix: File, relTool: String, arg: String): String {
        return try {
            val pb = ProcessBuilder(File(prefix, relTool).absolutePath, arg)
            pb.environment()["LD_LIBRARY_PATH"] =
                prefix.absolutePath + ":" + File(prefix, "lib").absolutePath
            pb.redirectErrorStream(true)
            val p = pb.start()
            val out = p.inputStream.bufferedReader().readText()
            val done = p.waitFor(10, java.util.concurrent.TimeUnit.SECONDS)
            if (!done || p.exitValue() != 0) return "unknown"
            p.destroy()
            out.lineSequence().firstOrNull { it.isNotBlank() } ?: "unknown"
        } catch (_: Throwable) {
            "unknown"
        }
    }

    /** Best-effort applet inventory; never throws (returns -1 when unknown). */
    private fun countApplets(busybox: File): Int {
        return try {
            val pb = ProcessBuilder(busybox.absolutePath, "--list")
            val prefix = busybox.parentFile
            pb.environment()["LD_LIBRARY_PATH"] =
                prefix.absolutePath + ":" + File(prefix, "lib").absolutePath
            pb.redirectErrorStream(true)
            val p = pb.start()
            val out = p.inputStream.bufferedReader().readText()
            val done = p.waitFor(5, java.util.concurrent.TimeUnit.SECONDS)
            if (!done) { p.destroy(); return -1 }
            if (p.exitValue() != 0) return -1
            out.lines().count { it.isNotBlank() }
        } catch (_: Throwable) {
            -1
        }
    }

    /**
     * Delete a stale userland tree but keep home/ (user data + Alpine guest)
     * and var/ (package DBs + manifests). Everything else is regenerated.
     */
    private fun wipeExceptHome(dir: File) {
        dir.listFiles()?.forEach { child ->
            if (child.name == "home" || child.name == "var") return@forEach
            try {
                if (child.isDirectory) child.deleteRecursively() else child.delete()
            } catch (_: Throwable) {
            }
        }
    }

    /** Mirror one APK asset subtree (bin/, lib/, …) into the userland. */
    private fun copyAssetTree(context: Context, assetPath: String, outDir: File) {
        val children = try {
            context.assets.list(assetPath)
        } catch (_: Exception) {
            null
        }
        if (children == null || children.isEmpty()) {
            // A file (or an empty dir): try a file copy, fall back to mkdir.
            try {
                outDir.parentFile?.mkdirs()
                copyAsset(context, assetPath, outDir)
                outDir.setReadable(true, false)
            } catch (_: Exception) {
                outDir.mkdirs()
            }
            return
        }
        outDir.mkdirs()
        for (child in children) {
            copyAssetTree(context, "$assetPath/$child", File(outDir, child))
        }
    }

    /** +x on everything under bin/ and libexec/ (scripts included). */
    private fun fixExecBits(dir: File) {
        for (tree in listOf("bin", "libexec")) {
            File(dir, tree).walkTopDown().forEach {
                try {
                    if (it.isFile) it.setExecutable(true, false)
                } catch (_: Throwable) {
                }
            }
        }
    }

    /**
     * Rewrite Termux-baked shebang lines (78 files: `#!/data/data/com.termux/
     * files/usr/bin/X`) to interpreters that exist here. Without this, npm,
     * yarn, git helpers, pydoc and friends die with "Permission denied".
     * `sh` maps straight to /system/bin/sh; everything else goes through
     * /system/bin/env so our $PREFIX/bin wins via PATH. Rewritten files get
     * +x (covers node_modules/.bin-style helpers fixExecBits never sees).
     * Returns the count rewritten (logged to the boot log).
     */
    private fun fixShebangs(dir: File): Int {
        var fixed = 0
        val prefix = "#!/data/data/com.termux/files/usr/bin/"
        val altPrefix = "#! /data/data/com.termux/files/usr/bin/"
        for (tree in listOf("bin", "lib", "libexec", "share", "etc")) {
            File(dir, tree).walkTopDown().forEach { file ->
                try {
                    if (!file.isFile || file.length() > 1_000_000) return@forEach
                    val bytes = file.readBytes()
                    val nl = bytes.indexOf('\n'.code.toByte())
                    if (nl < 0) return@forEach
                    var first = String(bytes, 0, nl, Charsets.UTF_8)
                    val tool: List<String>
                    when {
                        first.startsWith(prefix) -> first = first.removePrefix(prefix)
                        first.startsWith(altPrefix) -> first = first.removePrefix(altPrefix)
                        else -> return@forEach
                    }
                    tool = first.split(Regex("\\s+")).filter { it.isNotEmpty() }
                    if (tool.isEmpty()) return@forEach
                    // Drop a leading `env` (it only re-dispatches by PATH);
                    // route everything else through env so $PREFIX/bin wins.
                    val cmd = if (tool[0] == "env") tool.drop(1) else tool
                    if (cmd.isEmpty()) return@forEach
                    val replacement = if (cmd[0] == "sh") {
                        "#!/system/bin/sh " + cmd.drop(1).joinToString(" ").trim()
                    } else {
                        "#!/system/bin/env " + cmd.joinToString(" ")
                    }
                    val rest = if (nl + 1 < bytes.size) {
                        bytes.copyOfRange(nl, bytes.size)
                    } else {
                        "\n".toByteArray()
                    }
                    val out = replacement.toByteArray() + rest
                    file.writeBytes(out)
                    file.setExecutable(true, false)
                    fixed++
                } catch (_: Throwable) {
                }
            }
        }
        return fixed
    }

    /**
     * Recreate the .deb symlinks (bin/vim, lib .so chains, npm/npx, …) from
     * the symlinks.txt manifest. APKs cannot store symlinks portably, so they
     * ship as a manifest and are recreated here. Returns the count created.
     */
    private fun createSymlinks(context: Context, dir: File): Int {
        var made = 0
        val manifest = try {
            context.assets.open("$ASSET_DIR/symlinks.txt").bufferedReader().readLines()
        } catch (_: Exception) {
            return -1
        }
        for (line in manifest) {
            val parts = line.split(" -> ", limit = 2)
            if (parts.size != 2) continue
            val link = File(dir, parts[0].trim())
            val target = parts[1].trim()
            try {
                link.parentFile?.mkdirs()
                if (link.exists() || android.system.Os.lstat(link.absolutePath) != null) {
                    link.delete()
                }
            } catch (_: Throwable) {
            }
            try {
                android.system.Os.symlink(target, link.absolutePath)
                made++
            } catch (_: Throwable) {
            }
        }
        return made
    }

    private fun copyAsset(context: Context, assetPath: String, out: File) {
        context.assets.open(assetPath).use { input ->
            FileOutputStream(out).use { output ->
                input.copyTo(output)
            }
        }
    }
}
