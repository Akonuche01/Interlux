package com.keneristudios.interlux

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.os.BatteryManager
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel

/**
 * Device-API bridge, Termux:API-style (Phase 3.11).
 *
 * v1, no new permissions needed (targetSdk 28 grants notification +
 * vibration at install; clipboard reads show the OS toast themselves):
 * battery level, clipboard get/set, local notifications, haptic buzz.
 * Camera/TTS/location stay future items. Shell-CLI bindings are a separate
 * future item; this surface is for the app UI and the Stage-6 AI agent.
 */
class DeviceApi(messenger: BinaryMessenger, private val context: Context) :
    MethodChannel.MethodCallHandler {

    private val channel = MethodChannel(messenger, "interlux/device")

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
            else -> result.notImplemented()
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
