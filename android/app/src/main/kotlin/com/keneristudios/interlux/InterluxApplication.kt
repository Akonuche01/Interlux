package com.keneristudios.interlux

import android.app.Application

/**
 * Instruments startup and loads the native library off the main thread.
 *
 * The native library used to be loaded eagerly here with System.loadLibrary,
 * which runs on the main thread before any Activity exists. Any load failure
 * killed the process instantly with no opportunity to report it. Loading now
 * happens on a background thread via [NativeLib], and the result is surfaced
 * to Flutter as NATIVE_LIBRARY_FAILED when a terminal session starts.
 */
class InterluxApplication : Application() {
    override fun onCreate() {
        super.onCreate()

        // Wire up logging first, so everything below is observable from Termux.
        BootTracer.init(applicationContext)
        // Report the exact build to the public Downloads log so it is always
        // possible to tell which APK is actually running.
        try {
            val pi = applicationContext.packageManager
                .getPackageInfo(applicationContext.packageName, 0)
            BootTracer.stepPublic("version=${pi.versionName} build=${pi.versionCode}")
        } catch (_: Throwable) {
        }
        BootTracer.step("Application.onCreate")
        BootTracer.installCrashHandler()
        CrashLogger.install(applicationContext)

        // Load libinterlux.so off the main thread. A failure here is recorded
        // and reported to the user, never fatal.
        Thread {
            BootTracer.step("native load: starting")
            NativeLib.ensureLoaded()
            BootTracer.step(
                if (NativeLib.loadError == null) "native load: OK"
                else "native load: FAILED ${NativeLib.loadError}"
            )
        }.also { it.isDaemon = true; it.start() }
    }
}
