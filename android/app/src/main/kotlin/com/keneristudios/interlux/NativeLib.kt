package com.keneristudios.interlux

/**
 * Loads libinterlux.so exactly once, and records any failure.
 *
 * Loading must never happen on the main thread: a load failure there kills the
 * process before anything can report it. Callers off the main thread may call
 * [ensureLoaded] directly; the Application schedules it on a background thread
 * at startup so the result is usually ready before a terminal is opened.
 */
object NativeLib {
    @Volatile var loadError: String? = null
        private set

    @Volatile private var attempted = false

    /** Idempotent. Safe to call from any thread. */
    fun ensureLoaded() {
        if (attempted) return
        attempted = true
        try {
            System.loadLibrary("interlux")
            loadError = null
        } catch (t: Throwable) {
            loadError = "${t.javaClass.name}: ${t.message}"
        }
    }
}
