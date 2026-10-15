package com.jarvis.assistant.ui.assistant

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleEventEffect
import com.jarvis.assistant.HandsFreeStatus
import com.jarvis.assistant.ui.theme.Amber
import com.jarvis.assistant.ui.theme.Cyan
import com.jarvis.assistant.ui.theme.Danger
import com.jarvis.assistant.ui.theme.Navy
import com.jarvis.assistant.ui.theme.TextMuted
import com.jarvis.assistant.voice.HandsFreeService

/** Explicit Start/Stop for background listening, with its real status and battery guidance. */
@Composable
fun HandsFreeBar(status: HandsFreeStatus) {
    val context = LocalContext.current
    var problem by remember { mutableStateOf<String?>(null) }
    var refresh by remember { mutableIntStateOf(0) }
    LifecycleEventEffect(androidx.lifecycle.Lifecycle.Event.ON_RESUME) { refresh++ }
    val unrestricted = remember(refresh) {
        context.getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(context.packageName)
    }
    val needed = buildList {
        add(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
    }
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { result ->
        problem = when {
            result[Manifest.permission.RECORD_AUDIO] == false -> "Hands-free needs the microphone permission."
            result[Manifest.permission.POST_NOTIFICATIONS] == false ->
                "Allow notifications so the listening indicator is always visible."
            else -> {
                HandsFreeService.start(context)
                null
            }
        }
    }

    Surface(
        shape = RoundedCornerShape(14.dp),
        color = MaterialTheme.colorScheme.surfaceContainer,
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
    ) {
        Column(Modifier.padding(horizontal = 12.dp, vertical = 8.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("Hands-free", style = MaterialTheme.typography.labelSmall, color = TextMuted)
                    Text(
                        if (status.active) status.label else "Off — say “Jarvis …” after you start it",
                        style = MaterialTheme.typography.bodySmall,
                        color = if (status.active) Cyan else TextMuted,
                        maxLines = 2,
                    )
                }
                if (status.active) {
                    OutlinedButton(onClick = { HandsFreeService.stop(context) }) { Text("Stop", color = Danger) }
                } else {
                    Button(
                        onClick = {
                            problem = null
                            val missing = needed.filter { context.checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
                            if (missing.isEmpty()) HandsFreeService.start(context) else launcher.launch(missing.toTypedArray())
                        },
                        colors = ButtonDefaults.buttonColors(containerColor = Cyan, contentColor = Navy),
                    ) { Text("Start") }
                }
            }
            (problem ?: status.error)?.let { Text(it, color = Amber, style = MaterialTheme.typography.bodySmall) }
            if (!unrestricted) {
                Text(
                    "Battery optimization may stop listening in the background. On Xiaomi also set Battery saver to " +
                        "“No restrictions” and allow Autostart.",
                    color = TextMuted,
                    style = MaterialTheme.typography.bodySmall,
                )
                TextButton(onClick = { openBatterySettings(context) }) { Text("Battery settings") }
            }
        }
    }
}

private fun openBatterySettings(context: Context) {
    val app = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).setData(Uri.fromParts("package", context.packageName, null))
    val general = Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
    runCatching { context.startActivity(app) }.onFailure { runCatching { context.startActivity(general) } }
}
