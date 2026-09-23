package com.keneristudios.interlux

import android.os.Bundle
import com.keneristudios.interlux.pty.Pty
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine

class MainActivity : FlutterActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        BootTracer.step("MainActivity.onCreate")
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        BootTracer.step("configureFlutterEngine")
        TerminalService.start(applicationContext)
        PowerGate(flutterEngine.dartExecutor.binaryMessenger, applicationContext)
        Pty(flutterEngine.dartExecutor.binaryMessenger, applicationContext)
    }
}
