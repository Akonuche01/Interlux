package com.keneristudios.interlux

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.location.LocationManager
import android.os.BatteryManager
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.io.File
import java.util.Locale

/**
 * Device-API bridge, Termux:API-style (Phase 3.11).
 *
 * v1: battery level, clipboard get/set, local notifications, haptic buzz
 * (no new permissions on targetSdk 28; clipboard reads show the OS toast).
 * v2: TTS speak/stop (system engine, no permission), last-known location
 * (needs location permission — request via requestLocation), photo capture
 * delegated to the system camera app (no camera permission needed by us;
 * result delivered as a file path via MainActivity's launcher).
 * Shell-CLI bindings are a separate future item; this surface is for the
 * app UI and the Stage-6 AI agent.
 */
class DeviceApi(
    messenger: BinaryMessenger,
    private val context: Context,
    private val photoLauncher: (output: File, cb: (Boolean) -> Unit) -> Unit,
    private val locationRequester: (cb: (Boolean) -> Unit) -> Unit,
) : MethodChannel.MethodCallHandler {

    private val channel = MethodChannel(messenger, "interlux/device")
    private var tts: TextToSpeech? = null
    private var ttsReady = false
    private val ttsQueue = ArrayDeque<String>()

    init {
        channel.setMethodCallHandler(this)
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "batteryLevel" -> {
                val bm = context.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
                val level = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
                result.success(if (level >= 0) level else -1)
            }
            "clipboardGet" -> {
                val cm = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                val clip = cm.primaryClip
                if (clip == null || clip.itemCount == 0) {
                    result.success(null)
                } else {
                    result.success(clip.getItemAt(0).coerceToText(context).toString())
                }
            }
            "clipboardSet" -> {
                val text = call.argument<String>("text") ?: ""
                val cm = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                cm.setPrimaryClip(ClipData.newPlainText("interlux", text))
                result.success(true)
            }
            "notify" -> {
                val title = call.argument<String>("title") ?: "Interlux"
                val body = call.argument<String>("body") ?: ""
                sendNotification(title, body)
                result.success(true)
            }
            "vibrate" -> {
                val ms = (call.argument<Int>("ms") ?: 50).coerceIn(1, 2000).toLong()
                vibrate(ms)
                result.success(true)
            }
            "ttsSpeak" -> {
                speak(call.argument<String>("text") ?: "")
                result.success(true)
            }
            "ttsStop" -> {
                try {
                    tts?.stop()
                } catch (_: Throwable) {
                }
                result.success(true)
            }
            "lastLocation" -> {
                result.success(lastLocation())
            }
            "requestLocation" -> {
                locationRequester { granted ->
                    result.success(granted)
                }
            }
            "capturePhoto" -> {
                val out = File(
                    context.getExternalFilesDir(null),
                    "photo-${System.currentTimeMillis()}.jpg",
                )
                photoLauncher(out) { ok ->
                    result.success(if (ok && out.exists()) out.absolutePath else null)
                }
            }
            "openUrl" -> {
                result.success(openUrl(call.argument<String>("url") ?: ""))
            }
            else -> result.notImplemented()
        }
    }

    /**
     * Open an http(s) URL in the system browser. Anything else is refused —
     * callers (link tap) already filter, this is the second gate.
     */
    private fun openUrl(url: String): Boolean {
        return try {
            val uri = android.net.Uri.parse(url)
            val scheme = uri.scheme?.lowercase() ?: return false
            if (scheme != "http" && scheme != "https") return false
            if (uri.host.isNullOrEmpty()) return false
            val intent = android.content.Intent(
                android.content.Intent.ACTION_VIEW, uri
            ).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
            context.startActivity(intent)
            true
        } catch (_: Throwable) {
            false
        }
    }

    /** Fire-and-forget speech; engine init is async so early text queues. */
    private fun speak(text: String) {
        if (text.isBlank()) return
        try {
            val engine = tts ?: TextToSpeech(context) { status ->
                if (status == TextToSpeech.SUCCESS) {
                    ttsReady = true
                    tts?.language = Locale.getDefault()
                    val pending = ArrayList(ttsQueue)
                    ttsQueue.clear()
                    for (line in pending) speakNow(line)
                }
            }.also { tts = it }
            if (ttsReady) {
                speakNow(text)
            } else {
                ttsQueue.add(text)
                engine.toString()
            }
        } catch (_: Throwable) {
        }
    }

    private fun speakNow(text: String) {
        try {
            tts?.speak(text, TextToSpeech.QUEUE_ADD, null, "interlux-${System.nanoTime()}")
        } catch (_: Throwable) {
        }
    }

    /** Last-known fix from any provider; null when nothing cached/allowed. */
    private fun lastLocation(): Map<String, Double>? {
        return try {
            val lm = context.getSystemService(Context.LOCATION_SERVICE) as LocationManager
            val providers = lm.getProviders(true)
            var best: android.location.Location? = null
            for (name in providers) {
                try {
                    @Suppress("MissingPermission")
                    val loc = lm.getLastKnownLocation(name) ?: continue
                    if (best == null || (loc.time > best.time)) best = loc
                } catch (_: SecurityException) {
                    return null
                }
            }
            best?.let { mapOf("lat" to it.latitude, "lon" to it.longitude, "acc" to it.accuracy.toDouble()) }
        } catch (_: Throwable) {
            null
        }
    }

    private fun sendNotification(title: String, body: String) {
        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(
                    NOTES_CHANNEL,
                    "Interlux events",
                    NotificationManager.IMPORTANCE_DEFAULT,
                )
            )
        }
        val openApp = PendingIntent.getActivity(
            context, 0,
            context.packageManager.getLaunchIntentForPackage(context.packageName),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = Notification.Builder(context, NOTES_CHANNEL)
            .setContentTitle(title)
            .setContentText(body)
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentIntent(openApp)
            .setAutoCancel(true)
            .build()
        manager.notify(NOTES_ID++, notification)
    }

    private fun vibrate(ms: Long) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val vm = context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as VibratorManager
            vm.defaultVibrator.vibrate(
                VibrationEffect.createOneShot(ms, VibrationEffect.DEFAULT_AMPLITUDE)
            )
        } else {
            @Suppress("DEPRECATION")
            val vibrator = context.getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                vibrator.vibrate(
                    VibrationEffect.createOneShot(ms, VibrationEffect.DEFAULT_AMPLITUDE)
                )
            } else {
                @Suppress("DEPRECATION")
                vibrator.vibrate(ms)
            }
        }
    }

    companion object {
        const val NOTES_CHANNEL = "interlux-events"
        var NOTES_ID = 100
    }
}
