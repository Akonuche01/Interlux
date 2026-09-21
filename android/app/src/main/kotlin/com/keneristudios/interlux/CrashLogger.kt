package com.keneristudios.interlux

import android.content.Context
import java.io.File
import java.io.PrintWriter
import java.io.StringWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Writes uncaught exceptions to a file Termux can read.
 *
 * Android sandboxes logcat per-app, so a crash inside Interlux is invisible
 * from Termux's logcat. Instead we capture the stack trace here and drop it
 * in the app's external files dir, which is readable over shared storage.
 */
object CrashLogger {
    private const val FILE_NAME = "crash.log"

    fun install(context: Context) {
        val dir = context.getExternalFilesDir(null) ?: return
        val logFile = File(dir, FILE_NAME)

        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            try {
                val sw = StringWriter()
                throwable.printStackTrace(PrintWriter(sw))
                val ts = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date())
                logFile.appendText(
                    "\n===== $ts thread=${thread.name} =====\n$sw\n"
                )
            } catch (_: Throwable) {
                // Never let logging itself mask the original crash.
            }
            previous?.uncaughtException(thread, throwable)
        }
    }

    /** Read and clear the accumulated crash log. */
    fun readAndClear(context: Context): String {
        val dir = context.getExternalFilesDir(null) ?: return ""
        val logFile = File(dir, FILE_NAME)
        if (!logFile.exists()) return ""
        val text = logFile.readText()
        logFile.writeText("")
        return text
    }
}
