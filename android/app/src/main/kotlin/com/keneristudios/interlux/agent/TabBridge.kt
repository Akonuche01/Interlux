package com.keneristudios.interlux.agent

import android.content.Context
import android.util.Base64
import com.keneristudios.interlux.BootTracer
import com.keneristudios.interlux.pty.PtyHost
import java.io.BufferedReader
import java.io.File
import java.io.InputStreamReader
import java.io.PrintWriter
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.security.MessageDigest
import java.security.SecureRandom
import org.json.JSONArray
import org.json.JSONObject

/**
 * Loopback JSON-lines bridge into live terminal tabs, for the on-device
 * agent daemon (see agent/tools/tabs.py).
 *
 * One JSON object per line, one JSON response line back. Ops:
 *   {"op":"tabs"}                          -> {"tabs":[{"id":1,"alive":true}]}
 *   {"op":"read","id":1,"max_bytes":8192}  -> {"id":1,"alive":true,"data":"<base64>"}
 *   {"op":"send","id":1,"data":"<base64>"} -> {"ok":true} | {"error":"..."}
 *
 * `read` returns the tail of tapped output (TabBridge keeps none itself; the
 * Pty sessions hold the last 64KB each). `send` types raw bytes into the
 * live shell.
 *
 * AUTHENTICATION: every request must carry `token`, a per-install secret kept
 * in the app's private filesDir (see currentToken).
 *
 * This used to be an unauthenticated socket, and "bound to 127.0.0.1" was
 * never an access control: on Android every installed app shares the loopback
 * interface, so ANY of them could connect and use `send` to type arbitrary
 * bytes into a live shell running as our UID with the whole userland behind
 * it -- remote code execution for every app on the device. The old note
 * claiming "the daemon gates it behind user approval" described policy inside
 * our own agent, which an outside caller bypasses simply by speaking to this
 * socket directly.
 *
 * Started/stopped with the terminal service; failures are logged, never fatal.
 */
object TabBridge {
    const val PORT = 4601

    /** Per-install secret, inside the app's private filesDir. */
    private const val TOKEN_FILE = "tabbridge.token"

    @Volatile
    private var server: ServerSocket? = null

    @Volatile
    private var running = false

    @Volatile
    private var appContext: Context? = null

    private fun tokenFile(context: Context) = File(context.filesDir, TOKEN_FILE)

    /**
     * The current bridge secret, creating it on first use.
     *
     * It lives in filesDir, which no other UID can read, and the agent -- same
     * UID -- reads that exact same file (see agent/tools/tabs.py). That shared,
     * unreadable-by-others file is what makes the token a secret at all.
     *
     * Read fresh rather than cached in a field on purpose: Userland.ensure()
     * can replace the tree underneath us, and a cached token would then reject
     * the agent's own now-current token for the rest of the process lifetime.
     */
    @Synchronized
    private fun currentToken(context: Context): String {
        val file = tokenFile(context)
        val existing = try {
            file.readText().trim()
        } catch (_: Exception) {
            ""
        }
        if (existing.isNotEmpty()) return existing
        val bytes = ByteArray(32)
        SecureRandom().nextBytes(bytes)
        val fresh = Base64.encodeToString(bytes, Base64.NO_WRAP)
        try {
            file.parentFile?.mkdirs()
            file.writeText(fresh)
        } catch (e: Exception) {
            BootTracer.step("tabbridge: token write FAILED ${e.message}")
        }
        return fresh
    }

    fun ensure(context: Context) {
        val ctx = context.applicationContext
        appContext = ctx
        // Create the secret before the socket opens, so a client cannot arrive
        // while the token file is still missing and be rejected.
        currentToken(ctx)
        if (running) return
        synchronized(this) {
            if (running) return
            running = true
        }
        Thread({
            try {
                val ss = ServerSocket()
                ss.bind(InetSocketAddress("127.0.0.1", PORT))
                server = ss
                BootTracer.stepPublic("tabbridge: listening on 127.0.0.1:$PORT")
                while (running) {
                    try {
                        val client = ss.accept()
                        Thread({ handle(client) }).also {
                            it.isDaemon = true
                            it.start()
                        }
                    } catch (e: Exception) {
                        if (running) {
                            BootTracer.step("tabbridge: accept FAILED ${e.message}")
                        }
                    }
                }
            } catch (e: Exception) {
                BootTracer.stepPublic(
                    "tabbridge: FAILED ${e.javaClass.simpleName}: ${e.message}"
                )
                synchronized(this) { running = false }
            }
        }).also { it.isDaemon = true; it.start() }
    }

    fun stop() {
        synchronized(this) { running = false }
        try {
            server?.close()
        } catch (_: Exception) {
        }
        server = null
    }

    private fun handle(client: Socket) {
        try {
            client.use { s ->
                val reader = BufferedReader(InputStreamReader(s.getInputStream(), Charsets.UTF_8))
                val writer = PrintWriter(s.getOutputStream(), true)
                val line = reader.readLine() ?: return
                writer.println(answer(JSONObject(line)).toString())
            }
        } catch (_: Exception) {
        }
    }

    private fun answer(req: JSONObject): JSONObject {
        // Authenticate BEFORE touching any session -- see the class comment.
        // Constant-time compare, so the secret cannot be narrowed byte by byte
        // over repeated attempts. Length is part of what isEqual checks.
        val ctx = appContext
        if (ctx == null) return JSONObject().put("error", "bridge not ready")
        val expected = currentToken(ctx)
        val supplied = req.optString("token", "")
        if (!MessageDigest.isEqual(
                supplied.toByteArray(Charsets.UTF_8),
                expected.toByteArray(Charsets.UTF_8),
            )
        ) {
            BootTracer.step("tabbridge: rejected unauthorized request")
            return JSONObject().put("error", "unauthorized")
        }
        val pty = PtyHost.instance
        if (pty == null) return JSONObject().put("error", "no pty host yet")
        return when (req.optString("op")) {
            "tabs" -> {
                val arr = JSONArray()
                for (t in pty.listSessions()) {
                    arr.put(JSONObject().put("id", t.id).put("alive", t.alive))
                }
                JSONObject().put("tabs", arr)
            }
            "read" -> {
                val id = req.optInt("id", -1)
                val maxBytes = req.optInt("max_bytes", 8192).coerceIn(1, 65536)
                val raw = pty.tapBytes(id, maxBytes)
                JSONObject()
                    .put("id", id)
                    .put("alive", pty.sessionAlive(id))
                    .put(
                        "data",
                        Base64.encodeToString(raw, Base64.NO_WRAP),
                    )
            }
            "send" -> {
                val id = req.optInt("id", -1)
                val data = try {
                    Base64.decode(req.optString("data"), Base64.DEFAULT)
                } catch (_: Exception) {
                    return JSONObject().put("error", "bad base64")
                }
                if (pty.writeTo(id, data)) {
                    JSONObject().put("ok", true)
                } else {
                    JSONObject().put("error", "unknown or dead session: $id")
                }
            }
            else -> JSONObject().put("error", "unknown op")
        }
    }
}
