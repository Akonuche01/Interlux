package com.keneristudios.interlux

import android.os.Bundle
import com.keneristudios.interlux.pty.Pty
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine

class MainActivity : FlutterActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Install first, before anything that can crash.
        CrashLogger.install(applicationContext)
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        Pty(flutterEngine.dartExecutor.binaryMessenger, applicationContext)
    }
}
