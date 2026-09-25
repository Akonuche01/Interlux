package com.keneristudios.interlux.agent

import com.keneristudios.interlux.BootTracer
import java.io.File
import java.net.InetSocketAddress
import java.net.Socket

/**
 * Owns the on-device LLM backend (llama-server on :4602), which backs the
 * daemon's `local` provider.
 *
 * The binary comes from `pkg.sh install llama-cpp` (lazy, zero APK bloat)
 * and the model from `home/.models/`. If either is missing we log and skip —
 * the agent daemon still runs, and `local` turns fail cleanly with a
 * structured error instead of hanging.
 *
 * Same lifecycle as the agent: started from TerminalService, stopped with
 * it. Everything runs off the main thread; failures are logged, never fatal.
 */
object LlamaServer {
    const val PORT = 4602
    private const val MODEL_REL = "home/.models/qwen2.5-0.5b-q4.gguf"

    @Volatile
    private var starting = false

    fun ensure(context: android.content.Context) {
        if (isUp()) {
            BootTracer.step("llama: already up on :$PORT")
            return
        }
        synchronized(this) {
            if (isUp() || starting) return
            starting = true
        }
        try {
            val appContext = context.applicationContext
            val userland = File(appContext.filesDir, "userland")
            val server = File(userland, "bin/llama-server")
            val model = File(userland, MODEL_REL)
            if (!server.canExecute()) {
                BootTracer.stepPublic("llama: no server (pkg.sh install llama-cpp), skipping")
                return
            }
            if (!model.isFile) {
                BootTracer.stepPublic("llama: no model at $MODEL_REL, skipping")
                return
            }
            if (isUp()) {
                BootTracer.step("llama: already up on :$PORT")
                return
            }
            killStale(appContext)
            spawn(appContext, userland, server, model)
            if (!waitUp()) {
                BootTracer.stepPublic("llama: FAILED to answer on :$PORT")
                return
            }
            recordPid(appContext)
            BootTracer.stepPublic("llama: listening on 127.0.0.1:$PORT")
        } catch (e: Exception) {
            BootTracer.stepPublic(
                "llama: FAILED ${e.javaClass.simpleName}: ${e.message}"
            )
        } finally {
            synchronized(this) { starting = false }
        }
    }

    fun stop(context: android.content.Context) {
        try {
            val appContext = context.applicationContext
            val pidFile = pidFile(appContext)
            val pid = pidFile.takeIf { it.exists() }
                ?.readText()?.trim()?.toLongOrNull()
            if (pid != null && killPidIfServer(appContext, pid)) {
                BootTracer.step("llama: stopped pid=$pid")
            }
            pidFile.delete()
        } catch (e: Exception) {
            BootTracer.step("llama: stop FAILED ${e.message}")
        }
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

    /** Model load is slow; allow up to ~2 minutes. */
    private fun waitUp(): Boolean {
        repeat(48) {
            if (isUp()) return true
            Thread.sleep(2500)
        }
        return isUp()
    }

    private fun pidFile(context: android.content.Context): File {
        val home = File(File(context.filesDir, "userland"), "home")
        return File(home, ".interlux/agent/llama.pid")
    }

    /**
     * Best-effort pid bookkeeping for stop(): find our listener by cmdline.
     * Process.pid() needs API 26+ and minSdk is 24, so scan /proc instead.
     */
    private fun recordPid(context: android.content.Context) {
        try {
            val proc = File("/proc")
            val found = proc.listFiles()
                ?.filter { it.isDirectory && it.name.all { c -> c.isDigit() } }
                ?.firstOrNull { dir ->
                    try {
                        val cmd = File(dir, "cmdline").readBytes()
                            .toString(Charsets.UTF_8).replace('\u0000', ' ')
                        cmd.contains("llama-server") && cmd.contains(PORT.toString())
                    } catch (_: Exception) {
                        false
                    }
                }?.name?.toLongOrNull()
            if (found != null) {
                pidFile(context).also {
                    it.parentFile?.mkdirs()
                    it.writeText(found.toString())
                }
                BootTracer.step("llama: pid=$found")
            }
        } catch (_: Exception) {
        }
    }

    private fun killStale(context: android.content.Context) {
        try {
            val pid = pidFile(context).takeIf { it.exists() }
                ?.readText()?.trim()?.toLongOrNull()
                ?: return
            if (killPidIfServer(context, pid)) Thread.sleep(1000)
        } catch (_: Exception) {
        }
    }

    private fun killPidIfServer(context: android.content.Context, pid: Long): Boolean {
        return try {
            val cmdline = File("/proc/$pid/cmdline").takeIf { it.exists() }
                ?.readBytes()?.toString(Charsets.UTF_8)
                ?.replace('\u0000', ' ')
                ?.trim()
                .orEmpty()
            if (!cmdline.contains("llama-server")) return false
            val userland = File(context.filesDir, "userland")
            try {
                ProcessBuilder(
                    File(userland, "busybox").absolutePath,
                    "kill", "-9", pid.toString(),
                ).start().waitFor()
            } catch (_: Exception) {
            }
            true
        } catch (_: Exception) {
            false
        }
    }

    private fun spawn(
        context: android.content.Context,
        userland: File,
        server: File,
        model: File,
    ) {
        val pb = ProcessBuilder(
            server.absolutePath,
            "-m", model.absolutePath,
            "--host", "127.0.0.1",
            "--port", PORT.toString(),
            "-c", "2048",
        )
        val env = pb.environment()
        env["LD_LIBRARY_PATH"] = "${userland.absolutePath}/lib:${userland.absolutePath}"
        env["HOME"] = "${userland.absolutePath}/home"
        env["OPENSSL_CONF"] = "/dev/null"
        pb.directory(File(userland, "home"))
        val log = File(File(userland, "home"), ".interlux/agent/llama.log")
        log.parentFile?.mkdirs()
        pb.redirectOutput(ProcessBuilder.Redirect.appendTo(log))
        pb.redirectErrorStream(true)
        pb.start()
        BootTracer.step("llama: spawned for " + model.name)
    }
}
