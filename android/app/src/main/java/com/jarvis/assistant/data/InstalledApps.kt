package com.jarvis.assistant.data

import android.content.Context
import android.content.Intent
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.core.graphics.drawable.toBitmap

data class InstalledApp(val packageName: String, val label: String, val icon: ImageBitmap?)

/** Launchable apps on the device, sorted by name. Call off the main thread. */
fun loadInstalledApps(ctx: Context): List<InstalledApp> {
    val pm = ctx.packageManager
    val main = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
    return pm.queryIntentActivities(main, 0)
        .asSequence()
        .filter { it.activityInfo.packageName != ctx.packageName }
        .distinctBy { it.activityInfo.packageName }
        .map { ri ->
            InstalledApp(
                packageName = ri.activityInfo.packageName,
                label = ri.loadLabel(pm).toString(),
                icon = runCatching { ri.loadIcon(pm).toBitmap(96, 96).asImageBitmap() }.getOrNull(),
            )
        }
        .sortedBy { it.label.lowercase() }
        .toList()
}
