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
import java.io.IOException

/**
 * Bridges Flutter to a native pseudo-terminal.
 *
 * Lifecycle: [start] creates the pty and forks /system/bin/sh onto it, then a
 * reader thread pumps the shell's stdout/stderr up to Flutter over an
 * EventChannel. Input flows the other way via [write]. [stop] tears it all
 * down.
 *
 * The reader thread is a plain Thread, not a coroutine: reading from the pty
 * blocks by nature, and we want it to die quietly with the fd rather than
 * risk a coroutine cancellation racing the close.
 */
class Pty(messenger: BinaryMessenger, private val context: Context? = null) :
    MethodChannel.MethodCallHandler, EventChannel.StreamHandler {

    private val methodChannel = MethodChannel(messenger, "interlux/pty")
    private val eventChannel = EventChannel(messenger, "interlux/pty/events")

    private var fd: Int = -1
    private var readerThread: Thread? = null
    private var eventSink: EventChannel.EventSink? = null
    private val mainHandler = Handler(Looper.getMainLooper())

    init {
        methodChannel.setMethodCallHandler(this)
        eventChannel.setStreamHandler(this)
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
                    fd = nativeCreate()
                    if (fd < 0) {
                        result.error("PTY_CREATE_FAILED", "forkpty returned -1", null)
                        return
                    }
                    startReader()
                    result.success(fd)
                } catch (e: UnsatisfiedLinkError) {
                    result.error("NATIVE_LIBRARY_FAILED", e.message, null)
                } catch (e: Exception) {
                    result.error("PTY_CREATE_FAILED", e.message, null)
                }
            }
            "write" -> {
                val data = call.argument<ByteArray>("data")
                if (fd < 0 || data == null) {
                    result.error("PTY_NOT_OPEN", "terminal is not running", null)
                    return
                }
                try {
                    nativeWrite(fd, data, data.size)
                    result.success(null)
                } catch (e: Exception) {
                    result.error("PTY_WRITE_FAILED", e.message, null)
                }
            }
            "resize" -> {
                val cols = call.argument<Int>("cols") ?: 80
                val rows = call.argument<Int>("rows") ?: 24
                if (fd >= 0) {
                    nativeResize(fd, cols, rows)
                }
                result.success(null)
            }
            "stop" -> {
                stop()
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

    /**
     * Pump shell output to Flutter until EOF. Runs on a background thread.
     *
     * EventChannel sinks must be touched on the platform (main) thread; calling
     * them from this reader thread trips FlutterJNI's @UiThread check and kills
     * the process. Every sink hop therefore goes through [mainHandler].
     */
    private fun startReader() {
        readerThread = Thread {
            val buffer = ByteArray(8192)
            while (fd >= 0) {
                val n = try {
                    nativeRead(fd, buffer, buffer.size)
                } catch (e: Exception) {
                    break
                }
                if (n <= 0) break
                val chunk = if (n < buffer.size) buffer.copyOfRange(0, n) else buffer
                emit { it.success(chunk) }
            }
            emit { it.endOfStream() }
        }.also { it.start() }
    }

    /**
     * Run [block] against the current sink on the main thread. Copying the
     * reference first keeps a [stop] that clears the sink from racing the post.
     */
    private inline fun emit(crossinline block: (EventChannel.EventSink) -> Unit) {
        val sink = eventSink ?: return
        mainHandler.post { block(sink) }
    }

    fun stop() {
        fd = -1
        try {
            readerThread?.join(500)
        } catch (_: InterruptedException) {
        }
        emit { it.endOfStream() }
    }

    private external fun nativeCreate(): Int
    private external fun nativeRead(fd: Int, buf: ByteArray, len: Int): Int
    private external fun nativeWrite(fd: Int, buf: ByteArray, len: Int): Int
    private external fun nativeResize(fd: Int, cols: Int, rows: Int)
    private external fun nativeClose(fd: Int)

}
