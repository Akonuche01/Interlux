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

    fun step(message: String) {
        val ts = java.text.SimpleDateFormat("HH:mm:ss.SSS", java.util.Locale.US)
            .format(java.util.Date())
        val line = "$ts $message\n"

        // App-scoped external storage: no permission needed.
        try {
            val dir = File(Environment.getExternalStorageDirectory(),
                "Android/data/com.keneristudios.interlux/files")
            if (!dir.exists()) dir.mkdirs()
            File(dir, FILE).appendText(line)
        } catch (_: Throwable) {}

        // Public Downloads via MediaStore: readable from Termux.
        try {
            val values = ContentValues().apply {
                put("_display_name", FILE)
                put("mime_type", "text/plain")
                put("relative_path", "Download/")
                put("is_pending", 1)
            }
            val r = resolver ?: return
            val uri = r.insert(
                android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            if (uri != null) {
                r.openOutputStream(uri, "w")?.use { it.write(line.toByteArray()) }
                values.clear()
                values.put("is_pending", 0)
                r.update(uri, values, null, null)
            }
        } catch (_: Throwable) {}
    }

    fun installCrashHandler() {
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            try {
                val sw = StringWriter()
                throwable.printStackTrace(PrintWriter(sw))
                step("UNCAUGHT thread=${thread.name}\n$sw")
            } catch (_: Throwable) {}
            previous?.uncaughtException(thread, throwable)
        }
    }
}

class InterluxApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        BootTracer.init(applicationContext)
        BootTracer.installCrashHandler()
        BootTracer.step("Application.onCreate")

        // Load the native library here, at the earliest point, and record
        // whether it succeeds. A failure here is fatal but reportable.
        try {
            System.loadLibrary("interlux")
            BootTracer.step("loadLibrary(interlux) OK")
        } catch (t: Throwable) {
            BootTracer.step("loadLibrary(interlux) FAILED: ${t.javaClass.name}: ${t.message}")
        }
    }
}
