package com.keneristudios.interlux.agent

import android.content.Context
import com.keneristudios.interlux.BootTracer
import java.io.File

/**
 * Owns the trading bots' runtime (Kara move): long-lived guest processes
 * supervised independently of any UI, phone-call-safe.
 *
 * Why here and not in the bots' own shell loops: measured 2026-09-25, both
 * bot trees (supervisors included) died in one action, so only a process
 * outside that tree can restart them. The bots keep their own runner loops
 * (fxrun.sh/run.sh self-restart the brains); this supervisor owns the tree:
 * spawn it, know its pid, kill it, restart it when the watchdog finds it
 * gone.
 *
 * Bots run in the Alpine guest (python3 + pip deps proven on-device) via the
 * same proot shape as ssh-host.sh. Pids live under home/.interlux/agent/bots/
 * (wipe-proof). Boot autostart is OPT-IN via the bots.autostart file (bot
 * names, one per line) — a fresh install never starts trading bots by
 * surprise; the control surface manages the file explicitly.
 */
object BotSupervisor {

    data class Bot(val name: String, val runner: String)

    val BOTS = listOf(
        Bot("forexmind", "/root/bots/forexmind/fxrun.sh"),
        Bot("marketmind", "/root/bots/marketmind/run.sh"),
    )

    private fun dir(context: Context): File {
        return File(
            File(context.filesDir, "userland/home"),
            ".interlux/agent/bots",
        ).also { it.mkdirs() }
    }

    private fun pidFile(context: Context, name: String): File {
        return File(dir(context), "$name.pid")
    }

    private fun autostartFile(context: Context): File {
        return File(dir(context), "bots.autostart")
    }

    fun autostartNames(context: Context): List<String> {
        return try {
            autostartFile(context).takeIf { it.exists() }
                ?.readLines()?.map { it.trim() }
                ?.filter { it.isNotEmpty() && !it.startsWith("#") }
                ?: emptyList()
        } catch (_: Exception) {
            emptyList()
        }
    }

    fun setAutostart(context: Context, names: List<String>) {
        try {
            val known = BOTS.map { it.name }.toSet()
            val clean = names.map { it.trim() }.filter { it in known }
            autostartFile(context).writeText(clean.joinToString("\n"))
            BootTracer.step("bots: autostart=[${clean.joinToString(",")}]")
        } catch (e: Exception) {
            BootTracer.step("bots: autostart write FAILED ${e.message}")
        }
    }

    private fun pidOf(proc: Process): Long? {
        return try {
            Regex("pid=(\\d+)").find(proc.toString())
                ?.groupValues?.getOrNull(1)?.toLongOrNull()
        } catch (_: Exception) {
            null
        }
    }

    private fun cmdlineOf(pid: Long): String {
        return try {
            File("/proc/$pid/cmdline").takeIf { it.exists() }
                ?.readBytes()?.toString(Charsets.UTF_8)
                ?.replace('\u0000', ' ')?.trim().orEmpty()
        } catch (_: Exception) {
            ""
        }
    }

    /** True only if OUR runner for [name] is alive (cmdline-verified pid). */
    fun isRunning(context: Context, name: String): Boolean {
        val bot = BOTS.firstOrNull { it.name == name } ?: return false
        val pid = try {
            pidFile(context, name).takeIf { it.exists() }
                ?.readText()?.trim()?.toLongOrNull()
        } catch (_: Exception) {
            null
        } ?: return false
        val cmd = cmdlineOf(pid)
        return cmd.contains("proot") && cmd.contains(bot.runner)
    }

    fun status(context: Context): Map<String, Map<String, Any?>> {
        return BOTS.associate { bot ->
            val pid = try {
                pidFile(context, bot.name).takeIf { it.exists() }
                    ?.readText()?.trim()?.toLongOrNull()
            } catch (_: Exception) {
                null
            }
            bot.name to mapOf(
                "running" to isRunning(context, bot.name),
                "pid" to pid,
            )
        }
    }

    /**
     * Start [name] (or every known bot). Idempotent: running bots are left
     * alone. Returns per-bot {started, already, error}.
     */
    fun start(context: Context, name: String?): Map<String, Any?> {
        val appContext = context.applicationContext
        val targets = if (name.isNullOrBlank()) {
            BOTS
        } else {
            val bot = BOTS.firstOrNull { it.name == name }
                ?: return mapOf("error" to "unknown bot: $name")
            listOf(bot)
        }
        val out = mutableMapOf<String, Any?>()
        for (bot in targets) {
            if (isRunning(appContext, bot.name)) {
                out[bot.name] = mapOf("started" to false, "already" to true)
                continue
            }
            try {
                val userland = File(appContext.filesDir, "userland")
                val rootfs = File(userland, "home/.rootfs")
                if (!File(rootfs, "bin/busybox").exists()) {
                    out[bot.name] = mapOf(
                        "started" to false,
                        "error" to "no guest (run: rootfs.sh install)",
                    )
                    continue
                }
                val pb = ProcessBuilder(
                    File(userland, "proot").absolutePath,
                    "-r", rootfs.absolutePath,
                    "-0", "-b", "/dev", "-b", "/proc", "-b", "/sys",
                    "-w", "/root",
                    "/bin/busybox", "sh", bot.runner,
                )
                val env = pb.environment()
                env["LD_LIBRARY_PATH"] =
                    "${userland.absolutePath}:${userland.absolutePath}/lib"
                env["PROOT_TMP_DIR"] = "${userland.absolutePath}/tmp"
                env["PROOT_LOADER"] =
                    "${userland.absolutePath}/libexec/proot/loader"
                env["PROOT_LOADER_32"] =
                    "${userland.absolutePath}/libexec/proot/loader32"
                env["PROOT_NO_SECCOMP"] = "1"
                pb.directory(File(userland, "home"))
                pb.redirectOutput(
                    ProcessBuilder.Redirect.appendTo(
                        File(dir(appContext), "${bot.name}.log"),
                    ),
                )
                pb.redirectErrorStream(true)
                val proc = pb.start()
                val pid = pidOf(proc)
                if (pid != null) {
                    pidFile(appContext, bot.name).writeText(pid.toString())
                    BootTracer.step("bots: started ${bot.name} pid=$pid")
                    out[bot.name] = mapOf("started" to true, "pid" to pid)
                } else {
                    out[bot.name] = mapOf(
                        "started" to false, "error" to "no pid",
                    )
                }
            } catch (e: Exception) {
                BootTracer.step("bots: start ${bot.name} FAILED ${e.message}")
                out[bot.name] = mapOf(
                    "started" to false, "error" to e.message,
                )
            }
        }
        return out
    }

    /** Stop [name] (or every tracked bot) by verified pid. */
    fun stop(context: Context, name: String?): Map<String, Any?> {
        val appContext = context.applicationContext
        val targets = if (name.isNullOrBlank()) {
            BOTS.map { it.name }
        } else {
            listOf(name)
        }
        val out = mutableMapOf<String, Any?>()
        for (b in targets) {
            try {
                val pid = pidFile(appContext, b).takeIf { it.exists() }
                    ?.readText()?.trim()?.toLongOrNull()
                var killed = false
                if (pid != null) {
                    val cmd = cmdlineOf(pid)
                    if (cmd.contains("proot")) {
                        runQuiet(
                            File(File(appContext.filesDir, "userland"), "busybox").absolutePath,
                            "kill", "-9", pid.toString(),
                        )
                        killed = true
                    }
                }
                pidFile(appContext, b).delete()
                if (killed) BootTracer.step("bots: stopped $b pid=$pid")
                out[b] = mapOf("stopped" to killed)
            } catch (e: Exception) {
                out[b] = mapOf("stopped" to false, "error" to e.message)
            }
        }
        return out
    }

    /**
     * Restart autostart-listed bots that are not running. Called from the
     * watchdog tick and (opt-in) at boot. Never touches unlisted bots.
     */
    fun ensureRestart(context: Context) {
        val appContext = context.applicationContext
        for (name in autostartNames(appContext)) {
            try {
                if (!isRunning(appContext, name)) {
                    BootTracer.step("bots: watchdog restarting $name")
                    start(appContext, name)
                }
            } catch (e: Exception) {
                BootTracer.step("bots: watchdog $name FAILED ${e.message}")
            }
        }
    }

    private fun runQuiet(vararg cmd: String) {
        try {
            ProcessBuilder(*cmd).start().waitFor()
        } catch (_: Exception) {
        }
    }
}
