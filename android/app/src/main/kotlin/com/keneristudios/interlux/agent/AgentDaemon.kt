package com.keneristudios.interlux.agent

import android.content.Context
import com.keneristudios.interlux.BootTracer
import java.io.File
import java.net.InetSocketAddress
import java.net.Socket

/**
 * Owns the on-device AI agent daemon (iagent): starts it on the canonical
 * loopback port, keeps exactly one copy running, stops it with the service.
 *
 * Hooked from [com.keneristudios.interlux.TerminalService.onStartCommand],
 * so the daemon follows the same lifecycle as the shells: app launch, sticky
 * restart, and BOOT_COMPLETED all funnel through there. Everything runs off
 * the main thread; failures are logged, never fatal.
 *
 * The daemon itself lives in the extracted userland
 * (filesDir/userland/agent, bundled from the APK assets). Env mirrors
 * agent/iagent-launch exactly.
 */
object AgentDaemon {
    const val PORT = 4600

    @Volatile
    private var starting = false

    /**
     * Idempotent: returns once port 4600 answers. Spawns the daemon only if
     * nothing is listening. Safe to call from any thread.
     *
     * Freshness: a responder on the port is NOT enough. After an app
     * update the extracted userland is new while a reparented old daemon
     * may still hold the port -- keeping it would serve stale code
     * indefinitely, and that is exactly how old daemons outlived their
     * APKs. When the version marker changed under us, the old process
     * is killed and a fresh one spawned even though the port answers.
     */
    fun ensure(context: Context) {
        val appContext = context.applicationContext
        val marker = File(File(appContext.filesDir, "userland"), ".version")
        fun readMarker(): String = try {
            if (marker.exists()) marker.readText().trim() else ""
        } catch (_: Exception) {
            ""
        }
        if (isUp()) {
            // A responder alone is NOT enough: after an app update the
            // extracted userland is new while a reparented old daemon may
            // still hold the port -- keeping it would serve stale code
            // indefinitely, and that is exactly how old daemons outlived
            // their APKs. Userland.ensure is idempotent, so running it
            // here only detects the refresh, cheaply.
            val before = readMarker()
            com.keneristudios.interlux.userland.Userland.ensure(appContext)
            if (readMarker() == before) {
                BootTracer.step("agent: already up on :$PORT")
                return
            }
            BootTracer.stepPublic(
                "agent: userland refreshed ($before -> " +
                    "${readMarker()}), restarting daemon for fresh code")
            killStale(appContext)
        }
        synchronized(this) {
            if (starting) return
            starting = true
        }
        try {
            val userland =
                com.keneristudios.interlux.userland.Userland.ensure(appContext)
            if (isUp()) {
                BootTracer.step("agent: already up on :$PORT")
                return
            }
            // A concurrent extract can still be mid-wipe (fresh installs);
            // never spawn a daemon that cannot import. The next service
            // start retries.
            if (!File(userland, "agent/serve.py").exists() ||
                !File(userland, "site-packages/websockets/__init__.py").exists()
            ) {
                BootTracer.stepPublic("agent: userland incomplete, deferring")
                return
            }
            killStale(appContext)
            spawn(appContext, userland)
            if (!waitUp()) {
                BootTracer.stepPublic("agent: FAILED to answer on :$PORT")
                return
            }
            BootTracer.stepPublic("agent: listening on 127.0.0.1:$PORT")
        } catch (e: Exception) {
            BootTracer.stepPublic(
                "agent: FAILED ${e.javaClass.simpleName}: ${e.message}"
            )
        } finally {
            synchronized(this) { starting = false }
        }
    }

    /** Stop the daemon started by [ensure]. Manual debug daemons on other ports are left alone. */
    fun stop(context: Context) {
        try {
            val appContext = context.applicationContext
            val pidFile = pidFile(appContext)
            val pid = pidFile.takeIf { it.exists() }
                ?.readText()?.trim()?.toLongOrNull()
            if (pid != null && killPidIfAgent(appContext, pid)) {
                BootTracer.step("agent: stopped pid=$pid")
            }
            pidFile.delete()
        } catch (e: Exception) {
            BootTracer.step("agent: stop FAILED ${e.message}")
        }
    }

    /** Public status for the control surface. */
    fun isRunning(): Boolean = isUp()

    /** Our daemon pid, if the pid file names one. */
    fun pid(context: Context): Long? {
        return try {
            pidFile(context.applicationContext).takeIf { it.exists() }
                ?.readText()?.trim()?.toLongOrNull()
        } catch (_: Exception) {
            null
        }
    }

    fun waitUpPublic(): Boolean = waitUp()

    /** Fresh daemon (picks up changed agent files). Blocks briefly. */
    fun restart(context: Context) {
        stop(context)
        try {
            Thread.sleep(1000)
        } catch (_: Exception) {
        }
        ensure(context)
    }

    private fun isUp(): Boolean {
        return try {
            Socket().use { s ->
                s.connect(InetSocketAddress("127.0.0.1", PORT), 500)
                true
            }
        } catch (_: Exception) {
            false
        }
    }

    private fun waitUp(): Boolean {
        repeat(40) {
            if (isUp()) return true
            Thread.sleep(250)
        }
        return isUp()
    }

    private fun pidFile(context: Context): File {
        val home = File(File(context.filesDir, "userland"), "home")
        return File(home, ".interlux/agent/daemon.pid")
    }

    /**
     * Kill a previous daemon we started (pid file), if it is somehow still
     * alive but not answering. The cmdline is verified first so a recycled
     * pid can never kill an unrelated process. Never touches other ports.
     */
    private fun killStale(context: Context) {
        try {
            val pid = pidFile(context).takeIf { it.exists() }
                ?.readText()?.trim()?.toLongOrNull()
                ?: return
            if (killPidIfAgent(context, pid)) Thread.sleep(500)
        } catch (_: Exception) {
        }
    }

    /**
     * Kill [pid] only if its cmdline shows our agent (`-m agent -p 4600`).
     * Returns true if a kill was issued.
     */
    private fun killPidIfAgent(context: Context, pid: Long): Boolean {
        return try {
            val cmdline = File("/proc/$pid/cmdline").takeIf { it.exists() }
                ?.readBytes()?.toString(Charsets.UTF_8)
                ?.replace('\u0000', ' ')
                ?.trim()
                .orEmpty()
            if (!cmdline.contains("agent") || !cmdline.contains("4600")) return false
            val userland = File(context.filesDir, "userland")
            runQuiet(
                File(userland, "busybox").absolutePath,
                "kill", "-9", pid.toString(),
            )
            true
        } catch (_: Exception) {
            false
        }
    }

    /**
     * pid() needs API 26+ (minSdk is 24), so parse it out of the standard
     * "Process[pid=NNNN, ...]" toString instead. Null when unparseable.
     */
    private fun pidOf(proc: Process): Long? {
        return try {
            Regex("pid=(\\d+)").find(proc.toString())
                ?.groupValues?.getOrNull(1)?.toLongOrNull()
        } catch (_: Exception) {
            null
        }
    }

    private fun spawn(context: Context, userland: File) {
        val python = File(userland, "bin/python3").absolutePath
        val pb = ProcessBuilder(python, "-m", "agent", "-p", PORT.toString())
        val env = pb.environment()
        env["LD_LIBRARY_PATH"] = "${userland.absolutePath}/lib:${userland.absolutePath}"
        env["PYTHONHOME"] = userland.absolutePath
        env["PYTHONPATH"] =
            "${userland.absolutePath}/site-packages:${userland.absolutePath}"
        env["PATH"] = "${userland.absolutePath}/bin:${env["PATH"]}"
        env["OPENSSL_CONF"] = "/dev/null"
        env["SSL_CERT_FILE"] =
            "${userland.absolutePath}/etc/ssl/certs/ca-certificates.crt"
        env["HOME"] = "${userland.absolutePath}/home"
        pb.directory(File(userland, "home"))
        val log = File(
            File(userland, "home"),
            ".interlux/agent/daemon.log",
        )
        log.parentFile?.mkdirs()
        pb.redirectOutput(ProcessBuilder.Redirect.appendTo(log))
        pb.redirectErrorStream(true)
        val proc = pb.start()
        val pid = pidOf(proc)
        if (pid != null) {
            pidFile(context).also {
                it.parentFile?.mkdirs()
                it.writeText(pid.toString())
            }
        }
        val pidLabel = pid?.toString() ?: "unknown"
        BootTracer.step("agent: spawned pid=" + pidLabel)
    }

    private fun runQuiet(vararg cmd: String) {
        try {
            ProcessBuilder(*cmd).start().waitFor()
        } catch (_: Exception) {
        }
    }
}
