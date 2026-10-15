package com.jarvis.assistant.data

import android.content.Context
import com.jarvis.assistant.camera.CameraCoordinator
import com.jarvis.assistant.camera.CaptureResult
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import java.util.concurrent.atomic.AtomicLong

/** Runs device actions and keeps a log of every execution for the Task Execution screen. */
class TaskRepository(
    private val context: Context,
    private val settings: SettingsRepository,
    private val camera: CameraCoordinator,
) {
    private val ids = AtomicLong(System.currentTimeMillis())

    private val _tasks = MutableStateFlow<List<TaskRecord>>(emptyList())
    val tasks: StateFlow<List<TaskRecord>> = _tasks.asStateFlow()

    /**
     * Must be called on the main thread (starts activities). Returns null for "none" actions.
     * Plan steps pass log = false: the server's task history is their record, so they aren't listed twice.
     */
    fun execute(action: JarvisAction, source: String, log: Boolean = true): TaskRecord? {
        if (action.isNone) return null
        val result = ActionExecutor.execute(context, action, settings.blockedPackages.value)
        val record = TaskRecord(
            id = ids.incrementAndGet(),
            action = action,
            source = source,
            status = result.status,
            message = result.message,
            timestamp = System.currentTimeMillis(),
        )
        if (log) _tasks.update { listOf(record) + it.take(199) }
        return record
    }

    /** Like [execute], but also runs actions that need an in-app result (the camera). Main thread. */
    suspend fun run(action: JarvisAction, source: String, log: Boolean = true): TaskRecord? {
        if (action.type != "camera_capture") return execute(action, source, log)
        val (status, message) = when (val r = camera.capture()) {
            is CaptureResult.Saved -> TaskStatus.SUCCESS to "Photo saved to Pictures/JARVIS (${r.bytes / 1024} KB)"
            CaptureResult.Cancelled -> TaskStatus.CANCELLED to "Photo cancelled"
            CaptureResult.PermissionDenied -> TaskStatus.BLOCKED to "Camera permission denied"
            is CaptureResult.Failed -> TaskStatus.FAILED to r.reason
        }
        val record = TaskRecord(ids.incrementAndGet(), action, source, status, message, System.currentTimeMillis())
        if (log) _tasks.update { listOf(record) + it.take(199) }
        return record
    }

    /** Records an action that was handled elsewhere (voice actions, or refused by the server). */
    fun record(action: JarvisAction, source: String, status: TaskStatus, message: String): TaskRecord {
        val record = TaskRecord(ids.incrementAndGet(), action, source, status, message, System.currentTimeMillis())
        _tasks.update { listOf(record) + it.take(199) }
        return record
    }

    fun clear() {
        _tasks.value = emptyList()
    }
}
