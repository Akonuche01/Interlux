package com.keneristudios.interlux

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.MediaStore
import androidx.core.content.FileProvider
import com.keneristudios.interlux.pty.Pty
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import java.io.File

class MainActivity : FlutterActivity() {
    private var storageGate: StorageGate? = null
    private var photoCallback: ((Boolean) -> Unit)? = null
    private var photoFile: File? = null
    private var locationCallback: ((Boolean) -> Unit)? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        BootTracer.step("MainActivity.onCreate")
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        BootTracer.step("configureFlutterEngine")
        TerminalService.start(applicationContext)
        PowerGate(flutterEngine.dartExecutor.binaryMessenger, applicationContext)
        DeviceApi(
            flutterEngine.dartExecutor.binaryMessenger,
            applicationContext,
            photoLauncher = { output, cb ->
                photoCallback = cb
                photoFile = output
                val uri: Uri = FileProvider.getUriForFile(
                    this, "$packageName.files", output
                )
                val intent = Intent(MediaStore.ACTION_IMAGE_CAPTURE).apply {
                    putExtra(MediaStore.EXTRA_OUTPUT, uri)
                    addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
                }
                @Suppress("DEPRECATION")
                startActivityForResult(intent, PHOTO_REQUEST_CODE)
            },
            locationRequester = { cb ->
                locationCallback = cb
                androidx.core.app.ActivityCompat.requestPermissions(
                    this,
                    arrayOf(
                        android.Manifest.permission.ACCESS_FINE_LOCATION,
                        android.Manifest.permission.ACCESS_COARSE_LOCATION,
                    ),
                    LOCATION_REQUEST_CODE,
                )
            },
        )
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
        } else if (requestCode == LOCATION_REQUEST_CODE) {
            locationCallback?.invoke(
                grantResults.isNotEmpty() &&
                    grantResults[0] == android.content.pm.PackageManager.PERMISSION_GRANTED
            )
            locationCallback = null
        }
    }

    @Suppress("DEPRECATION")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == PHOTO_REQUEST_CODE) {
            val ok = resultCode == android.app.Activity.RESULT_OK &&
                (photoFile?.exists() == true)
            photoCallback?.invoke(ok)
            photoCallback = null
            photoFile = null
        }
    }

    companion object {
        const val LOCATION_REQUEST_CODE = 1002
        const val PHOTO_REQUEST_CODE = 1003
    }
}
