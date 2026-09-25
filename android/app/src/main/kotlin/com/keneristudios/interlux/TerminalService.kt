package com.keneristudios.interlux

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder

/**
 * Keeps the Interlux process (and every pty shell in it) alive while the
 * user is away from the app.
 *
 * Android kills background apps aggressively (we watched Doze throttle our
 * UID mid-test). A foreground service with a persistent notification tells
 * the system the user has ongoing work here; START_STICKY asks for a
 * restart if the process still dies. Type is specialUse: a terminal holding
 * user shells fits no other foreground-service category.
 */
class TerminalService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        BootTracer.step("TerminalService.onCreate")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            BootTracer.step("TerminalService.stop-requested")
            Thread {
                com.keneristudios.interlux.agent.AgentDaemon.stop(this)
                com.keneristudios.interlux.agent.TabBridge.stop()
                com.keneristudios.interlux.agent.LlamaServer.stop(this)
            }.also { it.isDaemon = true; it.start() }
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            return START_NOT_STICKY
        }
        startForegroundService();
        BootTracer.step("TerminalService.started")
        // Agent daemon + tab bridge + local LLM follow the service lifecycle
        // (launch, sticky restart, BOOT_COMPLETED all land here). Off the main
        // thread; failures are logged, never fatal.
        Thread {
            com.keneristudios.interlux.agent.AgentDaemon.ensure(this)
            com.keneristudios.interlux.agent.TabBridge.ensure()
            com.keneristudios.interlux.agent.LlamaServer.ensure(this)
        }.also { it.isDaemon = true; it.start() }
        return START_STICKY
    }

    private fun startForegroundService() {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_ID,
                    "Interlux terminal",
                    NotificationManager.IMPORTANCE_LOW,
                )
            )
        }

        val openApp = PendingIntent.getActivity(
            this, 0,
            packageManager.getLaunchIntentForPackage(packageName),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val stop = PendingIntent.getService(
            this, 1,
            Intent(this, TerminalService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val notification = Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Interlux terminal active")
            .setContentText("Your shells keep running while the app is in the background.")
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentIntent(openApp)
            .addAction(
                Notification.Action.Builder(null, "Stop", stop).build()
            )
            .setOngoing(true)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE,
            )
        } else {
            @Suppress("DEPRECATION")
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    companion object {
        const val CHANNEL_ID = "interlux-terminal"
        const val NOTIFICATION_ID = 1
        const val ACTION_STOP = "com.keneristudios.interlux.STOP_TERMINAL"

        fun start(context: Context) {
            val intent = Intent(context, TerminalService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }
    }
}
