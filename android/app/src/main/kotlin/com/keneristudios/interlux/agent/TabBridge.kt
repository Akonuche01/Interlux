package com.keneristudios.interlux.agent

import android.util.Base64
import com.keneristudios.interlux.BootTracer
import com.keneristudios.interlux.pty.PtyHost
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.PrintWriter
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
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
 * live shell — the daemon gates it behind user approval.
 *
 * Bound to 127.0.0.1 only, same sandbox as the agent. Started/stopped with
 * the terminal service; failures are logged, never fatal.
 */
object TabBridge {
    const val PORT = 4601

    @Volatile
    private var server: ServerSocket? = null

    @Volatile
    private var running = false

    fun ensure() {
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
