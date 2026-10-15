package com.jarvis.assistant

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import com.jarvis.assistant.ui.JarvisNavHost
import com.jarvis.assistant.ui.theme.JarvisTheme

class MainActivity : ComponentActivity() {
    private val permLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {}

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
        )
        super.onCreate(savedInstanceState)
        application.container.camera.attach(this)

        val missing = arrayOf(Manifest.permission.RECORD_AUDIO, Manifest.permission.READ_CONTACTS)
            .filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (savedInstanceState == null && missing.isNotEmpty()) permLauncher.launch(missing.toTypedArray())

        setContent {
            JarvisTheme { JarvisNavHost() }
        }
        handleDebugIntent(intent)
    }

    override fun onNewIntent(intent: android.content.Intent) {
        super.onNewIntent(intent)
        handleDebugIntent(intent)
    }

    /** adb shell am start -n com.jarvis.assistant/.MainActivity --es jarvis_command "open YouTube" (debug APK only). */
    private fun handleDebugIntent(intent: android.content.Intent?) {
        val debuggable = applicationInfo.flags and android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE != 0
        if (!debuggable || intent == null) return
        intent.getStringExtra("jarvis_command")?.let { DebugCommands.pending.value = it }
        when (intent.getStringExtra("handsfree")) { // started from this visible Activity, like the Start button
            "start" -> com.jarvis.assistant.voice.HandsFreeService.start(this)
            "stop" -> com.jarvis.assistant.voice.HandsFreeService.stop(this)
        }
        intent.removeExtra("jarvis_command")
        intent.removeExtra("handsfree")
    }

    override fun onDestroy() {
        application.container.camera.detach(this)
        super.onDestroy()
    }
}
