package com.keneristudios.interlux

import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel

/**
 * Shared-storage gate (Phase 1.3).
 *
 * With targetSdk 28 the classic storage permissions still give broad
 * /sdcard access (Termux's world). The shell exposes it as ~/storage
 * (symlink created by the login profile, no re-extract needed).
 */
class StorageGate(
    messenger: BinaryMessenger,
    private val activity: Activity,
) : MethodChannel.MethodCallHandler {

    private val channel = MethodChannel(messenger, "interlux/storage")
    private var pendingResult: MethodChannel.Result? = null

    init {
        channel.setMethodCallHandler(this)
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "hasStorage" -> {
                result.success(hasStorage())
            }
            "requestStorage" -> {
                if (hasStorage()) {
                    result.success(true)
                    return
                }
                pendingResult = result
                ActivityCompat.requestPermissions(
                    activity,
                    arrayOf(
                        Manifest.permission.READ_EXTERNAL_STORAGE,
                        Manifest.permission.WRITE_EXTERNAL_STORAGE,
                    ),
                    REQUEST_CODE,
                )
            }
            else -> result.notImplemented()
        }
    }

    fun onRequestPermissionsResult(granted: Boolean) {
        pendingResult?.success(granted)
        pendingResult = null
    }

    private fun hasStorage(): Boolean {
        return ContextCompat.checkSelfPermission(
            activity,
            Manifest.permission.READ_EXTERNAL_STORAGE,
        ) == PackageManager.PERMISSION_GRANTED
    }

    companion object {
        const val REQUEST_CODE = 1001
    }
}
