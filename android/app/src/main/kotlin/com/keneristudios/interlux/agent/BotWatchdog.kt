package com.keneristudios.interlux.agent

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.SystemClock
import com.keneristudios.interlux.BootTracer

/**
 * Watchdog tick for the trading bots (Kara move): re-ensure autostart-listed
 * bots every 15 minutes. Same proven pattern as Kara's EngineWatchdog — the
 * alarms live in OUR process so they survive the bots' tree dying — but the
 * tick runs fully in-process (receiver + goAsync, no service start, no
 * notification): it only checks pids and spawns what is missing.
 *
 * Alarms do not survive reboot; they are re-armed from TerminalService start
 * (which BootReceiver drives). Arming is idempotent.
 */
class BotWatchdog : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION_TICK) return
        val pending = goAsync()
        Thread {
            try {
                BotSupervisor.ensureRestart(context.applicationContext)
            } catch (e: Exception) {
                BootTracer.step("bots: watchdog tick FAILED ${e.message}")
            } finally {
                pending.finish()
            }
        }.also { it.isDaemon = true; it.start() }
    }

    companion object {
        const val ACTION_TICK = "com.keneristudios.interlux.BOTS_TICK"
        private const val REQUEST_CODE = 4601
        private const val INTERVAL_MS = 15 * 60 * 1000L

        fun arm(context: Context) {
            try {
                val alarms = context.getSystemService(Context.ALARM_SERVICE)
                    as? AlarmManager ?: return
                val intent = Intent(context, BotWatchdog::class.java).apply {
                    action = ACTION_TICK
                }
                val pi = PendingIntent.getBroadcast(
                    context, REQUEST_CODE, intent,
                    PendingIntent.FLAG_UPDATE_CURRENT or
                        PendingIntent.FLAG_IMMUTABLE,
                )
                alarms.setInexactRepeating(
                    AlarmManager.ELAPSED_REALTIME_WAKEUP,
                    SystemClock.elapsedRealtime() + INTERVAL_MS,
                    INTERVAL_MS,
                    pi,
                )
            } catch (e: Exception) {
                BootTracer.step("bots: watchdog arm FAILED ${e.message}")
            }
        }

        fun disarm(context: Context) {
            try {
                val alarms = context.getSystemService(Context.ALARM_SERVICE)
                    as? AlarmManager ?: return
                val intent = Intent(context, BotWatchdog::class.java).apply {
                    action = ACTION_TICK
                }
                val pi = PendingIntent.getBroadcast(
                    context, REQUEST_CODE, intent,
                    PendingIntent.FLAG_UPDATE_CURRENT or
                        PendingIntent.FLAG_IMMUTABLE,
                )
                alarms.cancel(pi)
            } catch (_: Exception) {
            }
        }
    }
}
