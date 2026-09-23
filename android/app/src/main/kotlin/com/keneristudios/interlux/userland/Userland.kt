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
    private const val VERSION = "full-tools-16"
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
            val links = createSymlinks(context, dir)
            marker.writeText(VERSION)
            com.keneristudios.interlux.BootTracer.stepPublic(
                "userland: assets copied, symlinks=$links"
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
     * share/, etc/). 4k+ files: node/python/git/nmap/openssh/vim + closure.
     */
    private val assetTrees = listOf("bin", "lib", "libexec", "share", "etc")

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
              R="${dir.absolutePath}/home/.rootfs"
              if [ ! -d "${'$'}R" ]; then echo "no guest: run rootfs.sh install first"; return 1; fi
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
     */
    private fun writePentestScript(dir: File) {
        File(dir, "pentest.sh").writeText(
            """
            #!/bin/sh
            # usage (inside guest): sh /root/pentest.sh tools|list|sqlmap
            # Rootless reality check: no raw sockets (nmap -sT only), no monitor
            # mode, no HID. Recon + web testing + scripting work fine.
            set -e
            cmd="${'$'}{1:-list}"
            case "${'$'}cmd" in
              tools)
                /root/pkginstall.sh nmap nmap-scripts python3 py3-pip git curl bash tmux bind-tools vim whois nikto hydra ffuf john tcpdump
                echo "guest tools ready — try: nmap -sT --version, nikto -Version"
                ;;
              sqlmap)
                pip install --break-system-packages sqlmap 2>/dev/null || pip install sqlmap
                sqlmap --version
                ;;
              list)
                echo "tools | sqlmap"
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
            DL=/tmp/pkgs
            DB=/var/lib/interlux-packages
            MDIR=/var/lib/interlux-files
            BB=/bin/busybox
            mkdir -p ${'$'}DL ${'$'}MDIR
            touch ${'$'}DB
            cmd="${'$'}{1:-list}"; shift || true
            refresh_indexes() {
              ${'$'}BB rm -f ${'$'}IDX_MAIN ${'$'}IDX_COMM
              ${'$'}BB wget -O ${'$'}IDX_MAIN ${'$'}MIRROR/main/aarch64/APKINDEX.tar.gz
              ${'$'}BB wget -O ${'$'}IDX_COMM ${'$'}MIRROR/community/aarch64/APKINDEX.tar.gz
            }
            [ -f ${'$'}IDX_MAIN ] || ${'$'}BB wget -O ${'$'}IDX_MAIN ${'$'}MIRROR/main/aarch64/APKINDEX.tar.gz
            [ -f ${'$'}IDX_COMM ] || ${'$'}BB wget -O ${'$'}IDX_COMM ${'$'}MIRROR/community/aarch64/APKINDEX.tar.gz
            dumpidx() {
              ${'$'}BB tar -xzOf ${'$'}IDX_MAIN APKINDEX 2>/dev/null
              ${'$'}BB tar -xzOf ${'$'}IDX_COMM APKINDEX 2>/dev/null
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
              soname=$(echo "${'$'}1" | ${'$'}BB cut -d: -f2 | ${'$'}BB cut -d= -f1)
              dumpidx | ${'$'}BB grep -B60 "p:[^ ]*${'$'}soname" | ${'$'}BB grep '^P:' | ${'$'}BB tail -n1 | ${'$'}BB cut -c3-
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
     * Delete a stale userland tree but keep home/ (user data + any downloaded
     * Alpine guest). Everything else is regenerated from APK assets.
     */
    private fun wipeExceptHome(dir: File) {
        dir.listFiles()?.forEach { child ->
            if (child.name == "home") return@forEach
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
