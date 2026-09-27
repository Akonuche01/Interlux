package com.keneristudios.interlux.agent

import android.app.Activity
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import com.keneristudios.interlux.BootTracer
import com.keneristudios.interlux.TerminalService

/**
 * Inter-app control surface for the agent daemon (Kara move-in, M6).
 *
 * Exported, guarded by the signature-level
 * `com.keneristudios.interlux.permission.CONTROL_AGENT` permission: only
 * apps signed with OUR key (Kara, built by us) can call it. Actions:
 * START_AGENT (make sure everything is up), STOP_AGENT (daemon down until
 * the next service start), RESTART_AGENT (fresh daemon, picks up new
 * agent/*.py without an app restart), AGENT_STATUS (is it answering?).
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
                stopSelf(startId)
            }
        }.also { it.isDaemon = true; it.start() }
        return START_NOT_STICKY
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
        const val PERMISSION = "com.keneristudios.interlux.permission.CONTROL_AGENT"
        const val ACTION_START_AGENT =
            "com.keneristudios.interlux.action.START_AGENT"
        const val ACTION_STOP_AGENT =
            "com.keneristudios.interlux.action.STOP_AGENT"
        const val ACTION_RESTART_AGENT =
            "com.keneristudios.interlux.action.RESTART_AGENT"
        const val ACTION_AGENT_STATUS =
            "com.keneristudios.interlux.action.AGENT_STATUS"
        const val EXTRA_RESULT =
            "com.keneristudios.interlux.extra.RESULT"
        const val RESULT_OK_KEY = "ok"
        const val RESULT_RUNNING_KEY = "running"
        const val RESULT_PID_KEY = "pid"
        const val RESULT_ERROR_KEY = "error"

        /** Explicit intent Kara (or adb) sends to drive the daemon. */
        fun intent(context: Context, action: String): Intent {
            return Intent(action).apply {
                setClassName(context.packageName,
                    "com.keneristudios.interlux.agent.AgentControl")
            }
        }
    }
}
