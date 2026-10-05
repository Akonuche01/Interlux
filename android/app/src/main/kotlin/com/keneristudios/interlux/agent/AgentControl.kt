package com.keneristudios.interlux.agent

import android.app.Activity
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
import com.keneristudios.interlux.BootTracer
import com.keneristudios.interlux.TerminalService

/**
 * Inter-app control surface for the agent daemon (client move-in, M6).
 *
 * Exported, guarded by the signature-level
 * `com.keneristudios.interlux.permission.CONTROL_AGENT` permission: only
 * apps signed with OUR key (our own client apps) can call it. Actions:
 * START_AGENT (make sure everything is up), STOP_AGENT (daemon down until
 * the next service start), RESTART_AGENT (fresh daemon, picks up changed
 * agent files without an app restart), AGENT_STATUS (is it answering?).
 *
 * Results go back through the caller's `EXTRA_RESULT` PendingIntent
 * (createPendingResult-style): extras {ok, running, pid, error}.
 * Every action is one-shot (START_NOT_STICKY) and off the main thread.
 */
class AgentControl : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action
        val replyTo = resultTo(intent)
        // Foreground promotion FIRST: callers include alarm receivers and adb,
        // where a background service start would otherwise be refused or
        // killed. MIN importance + removed on completion (usually <1s).
        promote()
        Thread {
            try {
                when (action) {
                    ACTION_START_AGENT -> {
                        TerminalService.start(this)
                        val up = AgentDaemon.waitUpPublic()
                        BootTracer.step("control: START_AGENT running=$up")
                        reply(replyTo, ok = true, running = up,
                            error = if (up) null else "daemon did not answer")
                    }
                    ACTION_STOP_AGENT -> {
                        AgentDaemon.stop(this)
                        BootTracer.step("control: STOP_AGENT")
                        reply(replyTo, ok = true, running = false)
                    }
                    ACTION_RESTART_AGENT -> {
                        AgentDaemon.restart(this)
                        val up = AgentDaemon.waitUpPublic()
                        BootTracer.step("control: RESTART_AGENT running=$up")
                        reply(replyTo, ok = true, running = up,
                            error = if (up) null else "daemon did not answer")
                    }
                    ACTION_AGENT_STATUS -> {
                        val up = AgentDaemon.isRunning()
                        reply(replyTo, ok = true, running = up)
                    }
                    ACTION_START_BOTS -> {
                        // Compat note: the old watchdog poked at an
                        // external runtime; ours supervises the relocated
                        // trees directly.
                        val bot = intent?.getStringExtra(EXTRA_BOT)
                        val res = BotSupervisor.start(this, bot)
                        val autostart = intent?.getBooleanExtra(
                            EXTRA_AUTOSTART, false) == true
                        if (autostart) {
                            val names = if (bot.isNullOrBlank()) {
                                BotSupervisor.BOTS.map { it.name }
                            } else {
                                listOf(bot)
                            }
                            val current = BotSupervisor.autostartNames(this)
                                .toMutableSet()
                            current.addAll(names)
                            BotSupervisor.setAutostart(this, current.toList())
                        }
                        BootTracer.step("control: START_BOTS $res")
                        replyMap(replyTo, ok = true, running = null,
                            extra = mapOf("bots" to res))
                    }
                    ACTION_STOP_BOTS -> {
                        val bot = intent?.getStringExtra(EXTRA_BOT)
                        val res = BotSupervisor.stop(this, bot)
                        BootTracer.step("control: STOP_BOTS $res")
                        replyMap(replyTo, ok = true, running = null,
                            extra = mapOf("bots" to res))
                    }
                    ACTION_BOTS_STATUS -> {
                        val res = BotSupervisor.status(this)
                        replyMap(replyTo, ok = true, running = null,
                            extra = mapOf("bots" to res))
                    }
                    ACTION_BOTS_AUTOSTART -> {
                        val names = intent?.getStringExtra(EXTRA_BOTS)
                            ?.split(",")?.map { it.trim() }
                            ?.filter { it.isNotEmpty() } ?: emptyList()
                        BotSupervisor.setAutostart(this, names)
                        replyMap(replyTo, ok = true, running = null,
                            extra = mapOf("autostart" to names))
                    }
                    ACTION_PAIRING_LIST -> {
                        pairCall(replyTo, "pairing/list", emptyMap())
                    }
                    ACTION_PAIRING_APPROVE -> {
                        val id = intent?.getStringExtra(EXTRA_PAIRING_ID)
                        if (id.isNullOrEmpty()) {
                            replyMap(replyTo, ok = false, running = null,
                                extra = mapOf("error" to "missing id"))
                        } else {
                            pairCall(replyTo, "pairing/approve",
                                mapOf("id" to id))
                        }
                    }
                    ACTION_PAIRING_DENY -> {
                        val id = intent?.getStringExtra(EXTRA_PAIRING_ID)
                        if (id.isNullOrEmpty()) {
                            replyMap(replyTo, ok = false, running = null,
                                extra = mapOf("error" to "missing id"))
                        } else {
                            pairCall(replyTo, "pairing/deny",
                                mapOf("id" to id))
                        }
                    }
                    ACTION_PAIRING_REVOKE -> {
                        val agent = intent?.getStringExtra(EXTRA_AGENT_ID)
                        if (agent.isNullOrEmpty()) {
                            replyMap(replyTo, ok = false, running = null,
                                extra = mapOf("error" to "missing agent_id"))
                        } else {
                            pairCall(replyTo, "pairing/revoke",
                                mapOf("agent_id" to agent))
                        }
                    }
                    else -> {
                        reply(replyTo, ok = false, running = false,
                            error = "unknown action: $action")
                    }
                }
            } catch (e: Exception) {
                BootTracer.step("control: FAILED ${e.message}")
                reply(replyTo, ok = false, running = false,
                    error = e.message)
            } finally {
                try {
                    stopForeground(STOP_FOREGROUND_REMOVE)
                } catch (_: Exception) {
                }
                stopSelf(startId)
            }
        }.also { it.isDaemon = true; it.start() }
        return START_NOT_STICKY
    }

    /** Pairing-plane call through our owner credential. Null payload +
     * ok=false means pairing is required first (another owner holds the
     * store) — the caller surfaces the Agents path, not an error log.
     * Wire failures arrive as ok=false with the daemon/RPC message. */
    private fun pairCall(to: PendingIntent?, method: String,
                         params: Map<String, Any?>) {
        try {
            val res = AgentAuth.rpc(this, method, params)
            if (res == null) {
                replyMap(to, ok = false, running = null,
                    extra = mapOf("error" to "pairing required"))
                return
            }
            @Suppress("UNCHECKED_CAST")
            val map = (res.keys().asSequence().toList())
                .associateWith { k -> res.opt(k) as Any? }
            replyMap(to, ok = true, running = null, extra = map)
        } catch (e: AgentWs.RpcError) {
            replyMap(to, ok = false, running = null,
                extra = mapOf("error" to (e.message ?: "rpc failed")))
        } catch (e: Exception) {
            replyMap(to, ok = false, running = null,
                extra = mapOf("error" to (e.message ?: "failed")))
        }
    }

    private fun reply(to: PendingIntent?, ok: Boolean, running: Boolean,
                      error: String? = null) {
        if (to == null) return
        try {
            val pid = AgentDaemon.pid(this)
            val fill = Intent().apply {
                putExtra(RESULT_OK_KEY, ok)
                putExtra(RESULT_RUNNING_KEY, running)
                if (pid != null) putExtra(RESULT_PID_KEY, pid)
                if (error != null) putExtra(RESULT_ERROR_KEY, error)
            }
            to.send(this, if (ok) Activity.RESULT_OK else Activity.RESULT_CANCELED, fill)
        } catch (_: Exception) {
        }
    }

    /** Reply variant for map payloads (bots supervision). Values must be
     * Bundle-safe (maps/lists of strings, booleans, longs). */
    private fun replyMap(to: PendingIntent?, ok: Boolean, running: Boolean?,
                         extra: Map<String, Any?>) {
        if (to == null) return
        try {
            val fill = Intent().apply {
                putExtra(RESULT_OK_KEY, ok)
                if (running != null) putExtra(RESULT_RUNNING_KEY, running)
                putExtra(RESULT_PAYLOAD_KEY, stringify(extra))
            }
            to.send(this, if (ok) Activity.RESULT_OK else Activity.RESULT_CANCELED, fill)
        } catch (_: Exception) {
        }
    }

    private fun stringify(value: Any?): String {
        return when (value) {
            null -> "null"
            is Map<*, *> -> value.entries.joinToString(",", "{", "}") { (k, v) ->
                "\"$k\":${stringify(v)}"
            }
            is List<*> -> value.joinToString(",", "[", "]") { stringify(it) }
            is String -> "\"$value\""
            else -> value.toString()
        }
    }

    private fun promote() {
        try {
            val manager =
                getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                manager.createNotificationChannel(
                    NotificationChannel(
                        CHANNEL_ID,
                        "Interlux control",
                        NotificationManager.IMPORTANCE_MIN,
                    ),
                )
            }
            val notification = Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("Interlux control")
                .setSmallIcon(android.R.drawable.stat_notify_sync)
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
        } catch (_: Exception) {
        }
    }

    private fun resultTo(intent: Intent?): PendingIntent? {
        if (intent == null) return null
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableExtra(EXTRA_RESULT, PendingIntent::class.java)
        } else {
            @Suppress("DEPRECATION")
            intent.getParcelableExtra(EXTRA_RESULT)
        }
    }

    companion object {
        const val CHANNEL_ID = "interlux-control"
        const val NOTIFICATION_ID = 2
        const val PERMISSION = "com.keneristudios.interlux.permission.CONTROL_AGENT"
        const val ACTION_START_AGENT =
            "com.keneristudios.interlux.action.START_AGENT"
        const val ACTION_STOP_AGENT =
            "com.keneristudios.interlux.action.STOP_AGENT"
        const val ACTION_RESTART_AGENT =
            "com.keneristudios.interlux.action.RESTART_AGENT"
        const val ACTION_AGENT_STATUS =
            "com.keneristudios.interlux.action.AGENT_STATUS"
        const val ACTION_START_BOTS =
            "com.keneristudios.interlux.action.START_BOTS"
        const val ACTION_STOP_BOTS =
            "com.keneristudios.interlux.action.STOP_BOTS"
        const val ACTION_BOTS_STATUS =
            "com.keneristudios.interlux.action.BOTS_STATUS"
        const val ACTION_BOTS_AUTOSTART =
            "com.keneristudios.interlux.action.BOTS_AUTOSTART"
        const val ACTION_PAIRING_LIST =
            "com.keneristudios.interlux.action.PAIRING_LIST"
        const val ACTION_PAIRING_APPROVE =
            "com.keneristudios.interlux.action.PAIRING_APPROVE"
        const val ACTION_PAIRING_DENY =
            "com.keneristudios.interlux.action.PAIRING_DENY"
        const val ACTION_PAIRING_REVOKE =
            "com.keneristudios.interlux.action.PAIRING_REVOKE"
        const val EXTRA_BOT =
            "com.keneristudios.interlux.extra.BOT"
        const val EXTRA_BOTS =
            "com.keneristudios.interlux.extra.BOTS"
        const val EXTRA_AUTOSTART =
            "com.keneristudios.interlux.extra.AUTOSTART"
        const val EXTRA_PAIRING_ID =
            "com.keneristudios.interlux.extra.PAIRING_ID"
        const val EXTRA_AGENT_ID =
            "com.keneristudios.interlux.extra.AGENT_ID"
        const val EXTRA_RESULT =
            "com.keneristudios.interlux.extra.RESULT"
        const val RESULT_OK_KEY = "ok"
        const val RESULT_RUNNING_KEY = "running"
        const val RESULT_PID_KEY = "pid"
        const val RESULT_ERROR_KEY = "error"
        const val RESULT_PAYLOAD_KEY = "payload"

        /** Explicit intent a paired client (or adb) sends to drive the daemon. */
        fun intent(context: Context, action: String): Intent {
            return Intent(action).apply {
                setClassName(context.packageName,
                    "com.keneristudios.interlux.agent.AgentControl")
            }
        }
    }
}
