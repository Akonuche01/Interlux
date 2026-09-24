package com.keneristudios.interlux

import android.os.Bundle
import com.keneristudios.interlux.pty.Pty
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine

class MainActivity : FlutterActivity() {
    private var storageGate: StorageGate? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        BootTracer.step("MainActivity.onCreate")
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        BootTracer.step("configureFlutterEngine")
        TerminalService.start(applicationContext)
        PowerGate(flutterEngine.dartExecutor.binaryMessenger, applicationContext)
        DeviceApi(flutterEngine.dartExecutor.binaryMessenger, applicationContext)
        storageGate = StorageGate(flutterEngine.dartExecutor.binaryMessenger, this)
        Pty(flutterEngine.dartExecutor.binaryMessenger, applicationContext)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == StorageGate.REQUEST_CODE) {
            storageGate?.onRequestPermissionsResult(
                grantResults.isNotEmpty() &&
                    grantResults[0] == android.content.pm.PackageManager.PERMISSION_GRANTED
            )
        }
    }
}
