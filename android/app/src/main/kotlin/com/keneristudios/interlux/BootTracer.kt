package com.keneristudios.interlux

import android.app.Application
import android.content.ContentValues
import android.net.Uri
import android.os.Environment
import java.io.File
import java.io.PrintWriter
import java.io.StringWriter

/**
 * Instruments the very first code the app process runs.
 *
 * The crash happens before anything can report it, so we log every lifecycle
 * step to a file the moment it happens. Whichever step is last in the file is
 * the one that killed us.
 *
 * Writes to the app's external files dir and mirrors into the public Downloads
 * folder via MediaStore so it is readable from outside the app sandbox.
 */
object BootTracer {
    private const val FILE = "interlux-boot.log"

    @Volatile private var resolver: android.content.ContentResolver? = null

    fun init(context: android.content.Context) {
        resolver = context.contentResolver
        step("BootTracer.init")
    }

    /**
     * Records one lifecycle step. Routine steps go to the app-scoped external
     * dir only, which needs no permission and stays out of the user's way.
     */
    fun step(message: String) {
        writeLine(formatLine(message), mirror = false)
    }

    /**
     * Like [step], but also mirrored into the public Downloads folder so it is
     * readable from outside the app sandbox (e.g. from Termux while
     * diagnosing the userland). Use for diagnostics only, never hot paths.
     */
    fun stepPublic(message: String) {
        writeLine(formatLine(message), mirror = true)
    }

    /**
     * Records a crash. This one IS mirrored into the public Downloads folder,
     * because a hard crash may take the process down before anything else can
     * surface it, and this is the file we read from Termux to diagnose it.
     */
    fun reportCrash(thread: Thread, throwable: Throwable) {
        val sw = StringWriter()
        throwable.printStackTrace(PrintWriter(sw))
        writeLine(formatLine("UNCAUGHT thread=${thread.name}\n$sw"), mirror = true)
    }

    private fun formatLine(message: String): String {
        val ts = java.text.SimpleDateFormat("HH:mm:ss.SSS", java.util.Locale.US)
            .format(java.util.Date())
        return "$ts $message\n"
    }

    private fun writeLine(line: String, mirror: Boolean) {
        // App-scoped external storage: no permission needed, invisible to the
        // user in their Downloads folder.
        try {
            val dir = File(Environment.getExternalStorageDirectory(),
                "Android/data/com.keneristudios.interlux/files")
            if (!dir.exists()) dir.mkdirs()
            File(dir, FILE).appendText(line)
        } catch (_: Throwable) {}

        if (!mirror) return

        // Public Downloads via MediaStore: append to ONE existing row if there
        // is one, instead of inserting a new file per call.
        try {
            val r = resolver ?: return
            val collection = android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI
            val projection = arrayOf(android.provider.MediaStore.MediaColumns._ID)
            val selection = "_display_name = ?"
            val selArgs = arrayOf(FILE)

            var uri: Uri? = null
            r.query(collection, projection, selection, selArgs, null)?.use { c ->
                if (c.moveToFirst()) {
                    uri = android.content.ContentUris.withAppendedId(
                        collection, c.getLong(0))
                }
            }

            if (uri == null) {
                val values = ContentValues().apply {
                    put("_display_name", FILE)
                    put("mime_type", "text/plain")
                    put("relative_path", "Download/")
                }
                uri = r.insert(collection, values)
            }

            uri?.let { u ->
                r.openOutputStream(u, "wa")?.use { it.write(line.toByteArray()) }
            }
        } catch (_: Throwable) {}
    }

    fun installCrashHandler() {
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            try {
                reportCrash(thread, throwable)
            } catch (_: Throwable) {}
            previous?.uncaughtException(thread, throwable)
        }
    }
}
