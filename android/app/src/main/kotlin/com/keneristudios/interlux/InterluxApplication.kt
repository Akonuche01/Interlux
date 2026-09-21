package com.keneristudios.interlux

import android.app.Application

/**
 * Deliberately does nothing.
 *
 * An earlier version eagerly called System.loadLibrary("interlux") here. That
 * runs on the main thread before any Activity exists, and any load failure
 * killed the process instantly with no opportunity to report it. The library
 * is now loaded lazily by [com.keneristudios.interlux.pty.Pty]'s companion
 * object, which only runs once a terminal session is actually requested.
 */
class InterluxApplication : Application()
