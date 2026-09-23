package com.keneristudios.interlux

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel

/**
 * Battery-optimizations gate (Phase 1.2).
 *
 * We watched Doze throttle our UID mid-test: screen off -> TCP timeouts,
 * DNS EPERM, guest apk dead. A foreground service keeps the process alive,
 * but only unrestricted battery use keeps the NETWORK alive in Doze.
 */
class PowerGate(messenger: BinaryMessenger, private val context: Context) :
    MethodChannel.MethodCallHandler {

    private val channel = MethodChannel(messenger, "interlux/power")

    init {
        channel.setMethodCallHandler(this)
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "isIgnoringBatteryOptimizations" -> {
                val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
                result.success(pm.isIgnoringBatteryOptimizations(context.packageName))
            }
            "requestIgnoreBatteryOptimizations" -> {
                try {
                    val intent = Intent(
                        android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        Uri.parse("package:${context.packageName}"),
                    ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    context.startActivity(intent)
                    result.success(true)
                } catch (e: Exception) {
                    result.error("NO_ACTIVITY", e.message, null)
                }
            }
            else -> result.notImplemented()
        }
    }
}
