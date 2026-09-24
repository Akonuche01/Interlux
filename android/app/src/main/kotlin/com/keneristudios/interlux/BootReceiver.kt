package com.keneristudios.interlux

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Boot persistence (Phase 3.12).
 *
 * Restarts the terminal service after reboot so shells survive restarts.
 * Semantics: the service runs iff the app was opened at least once since
 * boot (Android delivers BOOT_COMPLETED only to non-stopped apps, and our
 * Stop action halts it until the next launch). Uses BOOT_COMPLETED, not
 * LOCKED_BOOT_COMPLETED, because the userland lives in credential storage.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        BootTracer.step("BootReceiver: reboot detected")
        TerminalService.start(context.applicationContext)
    }
}
