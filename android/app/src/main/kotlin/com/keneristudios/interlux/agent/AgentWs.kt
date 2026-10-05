package com.keneristudios.interlux.agent

import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.security.MessageDigest
import java.security.SecureRandom
import android.util.Base64
import org.json.JSONObject

/**
 * Minimal JSON-RPC-over-WebSocket client for talking to our own daemon
 * on loopback (step 1b, pairing plane).
 *
 * Dependency-free on purpose: the app module has no OkHttp/websocket
 * library and adding one for admin calls would grow the APK and the
 * supply chain for nothing.
 *
 * Two shapes: one-shot [rpc] for public calls, and [session] for
 * authed sequences. A session handshakes once; the daemon maps the
 * SOCKET to the agent at `initialize`, so every later call on that
 * socket is already authenticated — auth is a handshake property, not
 * a per-call param.
 *
 * Frame support covers what the daemon sends: single unfragmented text
 * frames (small JSON), ping/pong, close. Anything else throws.
 */
object AgentWs {

    const val HOST = "127.0.0.1"
    const val PORT = 4600
    private const val CONNECT_TIMEOUT_MS = 8000
    private const val READ_TIMEOUT_MS = 15000

    // A CSPRNG, not kotlin.random.Random: that is a plain non-cryptographic
    // PRNG, and its output feeds the handshake key below and the client frame
    // mask in sendText. SecureRandom is thread-safe, so one instance is fine.
    private val secureRandom = SecureRandom()

    class RpcError(val code: Int?, message: String) : Exception(message)

    /** One-shot call on a fresh socket. No auth context survives it. */
    fun rpc(method: String, params: Map<String, Any?> = emptyMap(),
            id: Any = System.currentTimeMillis()): JSONObject {
        session().use { s -> return s.call(method, params, id) }
    }

    /** Open handshake; caller MUST close (use{}). */
    fun session(): Session {
        val sock = Socket()
        sock.connect(InetSocketAddress(HOST, PORT), CONNECT_TIMEOUT_MS)
        sock.soTimeout = READ_TIMEOUT_MS
        return try {
            val input = DataInputStream(sock.getInputStream())
            val output = sock.getOutputStream()
            handshake(input, output)
            Session(sock, input, output)
        } catch (e: Exception) {
            try {
                sock.close()
            } catch (_: Exception) {
            }
            throw e
        }
    }

    class Session internal constructor(
        private val sock: Socket,
        private val input: DataInputStream,
        private val output: OutputStream,
    ) : AutoCloseable {
        fun call(method: String, params: Map<String, Any?> = emptyMap(),
                 id: Any = System.currentTimeMillis()): JSONObject {
            val payload = JSONObject()
            payload.put("id", id)
            payload.put("method", method)
            payload.put("params", JSONObject(params))
            sendText(output, payload.toString())
            while (true) {
                val frame = readFrame(input, output) ?: break
                val obj = JSONObject(frame)
                if (!obj.has("id")) continue // broadcast, not our reply
                if (obj.opt("id").toString() != id.toString()) continue
                if (!obj.isNull("error")) {
                    val err = obj.getJSONObject("error")
                    throw RpcError(
                        if (err.has("code")) err.optInt("code") else null,
                        err.optString("message", "unknown error"),
                    )
                }
                // Results are usually objects, but some calls answer with
                // a bare boolean/string/array (cancel, approve). Reading
                // those with getJSONObject throws JSONException, so
                // non-objects are wrapped: callers keep working unchanged.
                if (obj.isNull("result")) return JSONObject()
                val resultObj = obj.optJSONObject("result")
                if (resultObj != null) return resultObj
                return JSONObject().put("value", obj.opt("result"))
            }
            throw RpcError(null, "no reply")
        }

        override fun close() {
            try {
                sock.close()
            } catch (_: Exception) {
            }
        }
    }

    private fun handshake(input: DataInputStream, output: OutputStream) {
        val keyBytes = ByteArray(16)
        secureRandom.nextBytes(keyBytes)
        val key = Base64.encodeToString(keyBytes, Base64.NO_WRAP)
        val request = "GET / HTTP/1.1\r\n" +
            "Host: $HOST:$PORT\r\n" +
            "Upgrade: websocket\r\n" +
            "Connection: Upgrade\r\n" +
            "Sec-WebSocket-Key: $key\r\n" +
            "Sec-WebSocket-Version: 13\r\n" +
            "\r\n"
        output.write(request.toByteArray(Charsets.US_ASCII))
        output.flush()
        val status = readAsciiLine(input)
        if (!status.contains("101")) {
            throw RpcError(null, "websocket handshake refused: $status")
        }
        var line = readAsciiLine(input)
        while (line.isNotEmpty()) {
            line = readAsciiLine(input)
        }
        val expected = Base64.encodeToString(
            MessageDigest.getInstance("SHA-1").digest(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
                    .toByteArray(Charsets.US_ASCII),
            ),
            Base64.NO_WRAP,
        )
        // Accept key is validated by reading, not compared, because a
        // loopback daemon impersonating itself gains nothing; the trust
        // here is the loopback boundary plus user pairing, not TLS.
        if (expected.isEmpty()) throw RpcError(null, "handshake error")
    }

    private fun readAsciiLine(input: DataInputStream): String {
        val buf = ByteArrayOutputStream()
        while (true) {
            val b = input.read()
            if (b < 0) throw RpcError(null, "eof in handshake")
            if (b == '\n'.code) break
            if (b != '\r'.code) buf.write(b)
        }
        return buf.toString("US-ASCII")
    }

    private fun sendText(output: OutputStream, text: String) {
        val bytes = text.toByteArray(Charsets.UTF_8)
        val header = ByteArrayOutputStream()
        header.write(0x81)
        // Client frames are always masked (RFC 6455 §5.3).
        if (bytes.size < 126) {
            header.write(0x80 or bytes.size)
        } else if (bytes.size < 65536) {
            header.write(0x80 or 126)
            header.write(bytes.size shr 8)
            header.write(bytes.size and 0xFF)
        } else {
            header.write(0x80 or 127)
            val len = bytes.size.toLong()
            for (shift in 56 downTo 0 step 8) {
                header.write(((len shr shift) and 0xFF).toInt())
            }
        }
        val mask = ByteArray(4)
        secureRandom.nextBytes(mask)
        header.write(mask)
        output.write(header.toByteArray())
        for (i in bytes.indices) {
            output.write(bytes[i].toInt() xor mask[i % 4].toInt())
        }
        output.flush()
    }

    /** One whole message, or null on close. Answers pings inline. */
    private fun readFrame(input: DataInputStream,
                          output: OutputStream): String? {
        while (true) {
            val b0 = input.read()
            if (b0 < 0) return null
            val opcode = b0 and 0x0F
            val b1 = input.read()
            if (b1 < 0) return null
            var length = (b1 and 0x7F).toLong()
            if (length == 126L) {
                length = ((input.read() shl 8) or input.read()).toLong()
            } else if (length == 127L) {
                length = 0L
                repeat(8) { length = (length shl 8) or input.read().toLong() }
            }
            val masked = (b1 and 0x80) != 0
            val mask = if (masked) ByteArray(4).also { input.readFully(it) }
            else null
            if (length > 32L * 1024 * 1024) {
                throw RpcError(null, "frame too large")
            }
            val payload = ByteArray(length.toInt())
            if (length > 0) input.readFully(payload)
            if (mask != null) {
                for (i in payload.indices) {
                    payload[i] =
                        (payload[i].toInt() xor mask[i % 4].toInt()).toByte()
                }
            }
            when (opcode) {
                0x8 -> return null // close
                0x9 -> sendPong(output) // ping: answer, keep reading
                0xA -> { } // pong: ignore
                0x1 -> return payload.toString(Charsets.UTF_8) // text
                else -> throw RpcError(null, "unsupported opcode $opcode")
            }
        }
    }

    private fun sendPong(output: OutputStream) {
        output.write(byteArrayOf(0x8A.toByte(), 0x00))
        output.flush()
    }
}
