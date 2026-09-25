package com.keneristudios.interlux.pty

import android.content.Context
import android.os.Handler
import android.os.Looper
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import com.keneristudios.interlux.CrashLogger
import com.keneristudios.interlux.NativeLib
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.util.ArrayDeque

/**
 * Bridges Flutter to native pseudo-terminals — one real session per tab.
 *
 * Each [start] forks a fresh shell onto its own pty and returns a session id.
 * Output events are tagged maps ({id, data} or {id, end}) on the single
 * broadcast EventChannel; the Dart side demultiplexes by id. This used to be
 * one global fd, so every tab mirrored the same shell — see terminal tabs.
 *
 * The reader thread is a plain Thread, not a coroutine: reading from the pty
 * blocks by nature, and we want it to die quietly with the fd rather than
 * risk a coroutine cancellation racing the close.
 *
 * Live sessions are visible to [com.keneristudios.interlux.agent.TabBridge]
 * through [PtyHost] so the on-device agent can attach to real tabs.
 */
class Pty(messenger: BinaryMessenger, private val context: Context? = null) :
    MethodChannel.MethodCallHandler, EventChannel.StreamHandler {

    private val methodChannel = MethodChannel(messenger, "interlux/pty")
    private val eventChannel = EventChannel(messenger, "interlux/pty/events")

    private data class Session(
        val id: Int,
        var fd: Int,
        var reader: Thread? = null,
        @Volatile var alive: Boolean = true,
        val tap: ArrayDeque<ByteArray> = ArrayDeque(),
        var tapBytes: Int = 0,
    )

    private val sessions = mutableMapOf<Int, Session>()
    private var nextId = 1
    private var eventSink: EventChannel.EventSink? = null
    private val mainHandler = Handler(Looper.getMainLooper())

    companion object {
        /** Bytes of recent output kept per session for bridge reads. */
        const val TAP_CAP = 65536
    }

    init {
        methodChannel.setMethodCallHandler(this)
        eventChannel.setStreamHandler(this)
        PtyHost.instance = this
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "start" -> {
                NativeLib.ensureLoaded()
                NativeLib.loadError?.let {
                    result.error("NATIVE_LIBRARY_FAILED", it, null)
                    return
                }
                try {
                    // Extract the bundled bionic userland (busybox) so the
                    // child shell can be our own POSIX environment rather than
                    // the bare Android system shell.
                    var userlandPath: String? = null
                    if (context != null) {
                        try {
                            userlandPath =
                                com.keneristudios.interlux.userland.Userland
                                    .ensure(context).absolutePath
                        } catch (e: Exception) {
                            CrashLogger.append(
                                context,
                                "userland extract failed: ${e.message}\n"
                            )
                        }
                    }
                    com.keneristudios.interlux.BootTracer
                        .stepPublic("pty: start userland=$userlandPath")
                    val fd = nativeCreate(userlandPath)
                    if (fd < 0) {
                        result.error("PTY_CREATE_FAILED", "forkpty returned -1", null)
                        return
                    }
                    val session = synchronized(sessions) {
                        val s = Session(id = nextId++, fd = fd)
                        sessions[s.id] = s
                        s
                    }
                    startReader(session)
                    result.success(session.id)
                } catch (e: UnsatisfiedLinkError) {
                    result.error("NATIVE_LIBRARY_FAILED", e.message, null)
                } catch (e: Exception) {
                    result.error("PTY_CREATE_FAILED", e.message, null)
                }
            }
            "write" -> {
                val id = call.argument<Int>("id") ?: -1
                val data = call.argument<ByteArray>("data")
                val session = synchronized(sessions) { sessions[id] }
                if (session == null || data == null) {
                    result.error("PTY_NOT_OPEN", "no such session: $id", null)
                    return
                }
                try {
                    nativeWrite(session.fd, data, data.size)
                    result.success(null)
                } catch (e: Exception) {
                    result.error("PTY_WRITE_FAILED", e.message, null)
                }
            }
            "resize" -> {
                val id = call.argument<Int>("id") ?: -1
                val cols = call.argument<Int>("cols") ?: 80
                val rows = call.argument<Int>("rows") ?: 24
                synchronized(sessions) { sessions[id] }?.let {
                    if (it.alive) nativeResize(it.fd, cols, rows)
                }
                result.success(null)
            }
            "stop" -> {
                val id = call.argument<Int>("id") ?: -1
                stopSession(id)
                result.success(null)
            }
            "getCrashLog" -> {
                val ctx = context
                result.success(if (ctx != null) CrashLogger.readAndClear(ctx) else "")
            }
            else -> result.notImplemented()
        }
    }

    override fun onListen(arguments: Any?, sink: EventChannel.EventSink?) {
        eventSink = sink
    }

    override fun onCancel(arguments: Any?) {
        eventSink = null
    }

    // -- Live-session surface for TabBridge ---------------------------------

    data class SessionInfo(val id: Int, val alive: Boolean)

    fun listSessions(): List<SessionInfo> =
        synchronized(sessions) { sessions.values.map { SessionInfo(it.id, it.alive) } }

    /** Last [maxBytes] of tapped output for [id], oldest first. Empty when unknown. */
    fun tapBytes(id: Int, maxBytes: Int): ByteArray {
        val session = synchronized(sessions) { sessions[id] } ?: return ByteArray(0)
        synchronized(session) {
            val out = ByteArrayOutputStream()
            var remaining = maxBytes.coerceAtLeast(0)
            // Chunks are stored oldest-first; take from the tail.
            val chunks = session.tap.toList()
            var start = chunks.size
            var take = 0
            var i = chunks.size - 1
            while (i >= 0 && take < remaining) {
                take += chunks[i].size
                start = i
                i--
            }
            for (j in start until chunks.size) {
                val c = chunks[j]
                if (remaining <= 0) break
                if (c.size <= remaining) {
                    out.write(c)
                    remaining -= c.size
                } else {
                    out.write(c, c.size - remaining, remaining)
                    remaining = 0
                }
            }
            return out.toByteArray()
        }
    }

    fun sessionAlive(id: Int): Boolean =
        synchronized(sessions) { sessions[id]?.alive } ?: false

    /** Write raw bytes into a live session. False when the session is unknown or dead. */
    fun writeTo(id: Int, data: ByteArray): Boolean {
        val session = synchronized(sessions) { sessions[id] } ?: return false
        if (!session.alive) return false
        return try {
            nativeWrite(session.fd, data, data.size)
            true
        } catch (e: Exception) {
            false
        }
    }

    // -- Internals ------------------------------------------------------------

    /**
     * Pump shell output to Flutter until EOF. Runs on a background thread.
     *
     * EventChannel sinks must be touched on the platform (main) thread; calling
     * them from this reader thread trips FlutterJNI's @UiThread check and kills
     * the process. Every sink hop therefore goes through [mainHandler].
     */
    private fun startReader(session: Session) {
        session.reader = Thread {
            val buffer = ByteArray(8192)
            while (session.alive) {
                val n = try {
                    nativeRead(session.fd, buffer, buffer.size)
                } catch (e: Exception) {
                    break
                }
                if (n <= 0) break
                val chunk = if (n < buffer.size) buffer.copyOfRange(0, n) else buffer.copyOf()
                appendTap(session, chunk)
                emit { it.success(mapOf("id" to session.id, "data" to chunk)) }
            }
            session.alive = false
            emit { it.success(mapOf("id" to session.id, "end" to true)) }
        }.also { it.start() }
    }

    private fun appendTap(session: Session, chunk: ByteArray) {
        synchronized(session) {
            session.tap.addLast(chunk)
            session.tapBytes += chunk.size
            while (session.tapBytes > TAP_CAP && session.tap.isNotEmpty()) {
                session.tapBytes -= session.tap.removeFirst().size
            }
        }
    }

    /**
     * Run [block] against the current sink on the main thread. Copying the
     * reference first keeps a [stop] that clears the sink from racing the post.
     */
    private inline fun emit(crossinline block: (EventChannel.EventSink) -> Unit) {
        val sink = eventSink ?: return
        mainHandler.post { block(sink) }
    }

    private fun stopSession(id: Int) {
        // Close the master fd BEFORE joining. The reader thread is blocked
        // inside read() on this fd; closing it is what unblocks the read so
        // the thread can actually exit. Native close also reaps the child.
        val session = synchronized(sessions) { sessions.remove(id) } ?: return
        session.alive = false
        val toClose = session.fd
        session.fd = -1
        if (toClose >= 0) {
            try {
                nativeClose(toClose)
            } catch (_: Throwable) {
            }
        }
        try {
            session.reader?.join(1000)
        } catch (_: InterruptedException) {
        }
        emit { it.success(mapOf("id" to id, "end" to true)) }
    }

    private external fun nativeCreate(userlandPath: String?): Int
    private external fun nativeRead(fd: Int, buf: ByteArray, len: Int): Int
    private external fun nativeWrite(fd: Int, buf: ByteArray, len: Int): Int
    private external fun nativeResize(fd: Int, cols: Int, rows: Int)
    private external fun nativeClose(fd: Int)

}

/**
 * Latest Pty plugin instance, for in-process bridges (TabBridge). Set on
 * construction; the plugin lives as long as the Flutter engine.
 */
object PtyHost {
    @Volatile
    var instance: Pty? = null
}
