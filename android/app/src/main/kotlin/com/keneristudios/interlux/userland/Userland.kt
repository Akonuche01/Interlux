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
 * The whole userland is two files:
 *   busybox              4 KB multicall launcher (interpreter /system/bin/linker64)
 *   libbusybox.so.1.38.0 876 KB, holds every applet
 * Standalone shell mode is compiled in, so `sh` resolves ls/cat/grep/... as
 * built-in applets without needing a forest of symlinks.
 */
object Userland {

    private const val VERSION = "busybox-1.38.0-1"
    private const val ASSET_DIR = "userland"
    private const val DIR_NAME = "userland"

    private val assets = listOf("busybox", "libbusybox.so.1.38.0")

    /** Files that must be executable. */
    private val executables = setOf("busybox")

    /**
     * Idempotent: returns the extracted userland dir, extracting (or
     * re-extracting after an app update) only when the version marker is stale.
     */
    fun ensure(context: Context): File {
        val dir = File(context.filesDir, DIR_NAME)
        if (!dir.exists()) dir.mkdirs()

        val marker = File(dir, ".version")
        if (marker.exists() && marker.readText().trim() == VERSION) {
            return dir
        }

        for (name in assets) {
            val out = File(dir, name)
            copyAsset(context, "$ASSET_DIR/$name", out)
            out.setReadable(true, false)
            if (name in executables) {
                out.setExecutable(true, false)
            }
        }
        marker.writeText(VERSION)
        return dir
    }

    private fun copyAsset(context: Context, assetPath: String, out: File) {
        context.assets.open(assetPath).use { input ->
            FileOutputStream(out).use { output ->
                input.copyTo(output)
            }
        }
    }
}
