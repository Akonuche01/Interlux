package com.keneristudios.interlux

import com.keneristudios.interlux.pty.Pty
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine

class MainActivity : FlutterActivity() {
    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        Pty(flutterEngine.dartExecutor.binaryMessenger)
    }
}
