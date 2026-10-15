package com.jarvis.assistant

import android.app.Activity
import android.app.Application
import android.content.Context
import android.os.Bundle
import com.jarvis.assistant.camera.CameraCoordinator
import com.jarvis.assistant.data.DispatchLedger
import com.jarvis.assistant.data.HistoryRepository
import com.jarvis.assistant.data.JarvisClient
import com.jarvis.assistant.data.SettingsRepository
import com.jarvis.assistant.data.TaskRepository
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow

/** App-wide singletons shared by every screen's ViewModel. */
class AppContainer(context: Context) {
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    val settings = SettingsRepository(context)
    val client = JarvisClient()
    val history = HistoryRepository(context, scope)
    val camera = CameraCoordinator(context)
    val tasks = TaskRepository(context, settings, camera)
    val ledger = DispatchLedger(context)

    /** True while a JARVIS screen is visible (Android only lets apps start other apps then). */
    val appVisible = MutableStateFlow(false)

    /** Background (hands-free) listening status, owned by HandsFreeService. */
    val handsFree = MutableStateFlow(HandsFreeStatus())
}

/** Debug-only command injection channel (see MainActivity.handleDebugIntent). */
object DebugCommands {
    val pending = MutableStateFlow<String?>(null)
}

data class HandsFreeStatus(val active: Boolean = false, val label: String = "Off", val error: String? = null)

class JarvisApp : Application() {
    lateinit var container: AppContainer
        private set

    override fun onCreate() {
        super.onCreate()
        container = AppContainer(this)
        registerActivityLifecycleCallbacks(object : ActivityLifecycleCallbacks {
            private var started = 0
            override fun onActivityStarted(activity: Activity) {
                started++
                container.appVisible.value = true
            }
            override fun onActivityStopped(activity: Activity) {
                started = (started - 1).coerceAtLeast(0)
                container.appVisible.value = started > 0
            }
            override fun onActivityCreated(activity: Activity, savedInstanceState: Bundle?) {}
            override fun onActivityResumed(activity: Activity) {}
            override fun onActivityPaused(activity: Activity) {}
            override fun onActivitySaveInstanceState(activity: Activity, outState: Bundle) {}
            override fun onActivityDestroyed(activity: Activity) {}
        })
    }
}

val Application.container: AppContainer get() = (this as JarvisApp).container
