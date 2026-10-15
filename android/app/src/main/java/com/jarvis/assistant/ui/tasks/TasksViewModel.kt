package com.jarvis.assistant.ui.tasks

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.jarvis.assistant.container
import com.jarvis.assistant.data.ActionExecutor
import com.jarvis.assistant.data.JarvisAction
import com.jarvis.assistant.data.PlanState
import com.jarvis.assistant.data.TaskRecord
import com.jarvis.assistant.data.TaskStatus
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class TasksUiState(
    val tasks: List<TaskRecord> = emptyList(),
    val selectedType: String = ActionExecutor.SUPPORTED.first(),
    val value: String = "",
    val succeeded: Int = 0,
    val failed: Int = 0,
    val blocked: Int = 0,
    val lastResult: TaskRecord? = null,
    val history: ServerHistory = ServerHistory(),
)

/** Plans recorded by the server (the durable execution history). */
data class ServerHistory(
    val loading: Boolean = false,
    val plans: List<PlanState> = emptyList(),
    val error: String? = null,
)

private data class Draft(val type: String, val value: String, val last: TaskRecord?)

class TasksViewModel(app: Application) : AndroidViewModel(app) {
    private val tasks = app.container.tasks
    private val client = app.container.client
    private val settings = app.container.settings
    private val draft = MutableStateFlow(Draft(ActionExecutor.SUPPORTED.first(), "", null))
    private val history = MutableStateFlow(ServerHistory())

    val uiState: StateFlow<TasksUiState> = combine(tasks.tasks, draft, history) { list, d, h ->
        TasksUiState(
            tasks = list,
            selectedType = d.type,
            value = d.value,
            succeeded = list.count { it.status == TaskStatus.SUCCESS },
            failed = list.count { it.status == TaskStatus.FAILED },
            blocked = list.count { it.status == TaskStatus.BLOCKED },
            lastResult = d.last,
            history = h,
        )
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), TasksUiState())

    init {
        refreshHistory()
    }

    fun refreshHistory() {
        history.update { it.copy(loading = true, error = null) }
        viewModelScope.launch {
            history.value = try {
                ServerHistory(plans = client.tasks(settings.serverUrl.value))
            } catch (e: Exception) {
                history.value.copy(loading = false, error = e.message ?: e.javaClass.simpleName)
            }
        }
    }

    fun selectType(type: String) = draft.update { it.copy(type = type) }

    fun setValue(value: String) = draft.update { it.copy(value = value) }

    fun runDraft() {
        val d = draft.value
        if (d.value.isBlank()) return
        val record = tasks.execute(JarvisAction(d.type, d.value.trim()), "manual")
        draft.update { it.copy(value = "", last = record) }
    }

    /** Capability-gated actions are re-authorized by the server before every re-run. */
    fun retry(record: TaskRecord) {
        val action = record.action
        if (action.type !in ActionExecutor.CAPABILITY_TYPES) {
            tasks.execute(action, "retry")
            return
        }
        viewModelScope.launch {
            val auth = try {
                client.authorizeAction(settings.serverUrl.value, action)
            } catch (e: Exception) {
                tasks.record(action, "retry", TaskStatus.BLOCKED, "Couldn't verify permission: ${e.message}")
                return@launch
            }
            when {
                !auth.allowed -> tasks.record(action, "retry", TaskStatus.BLOCKED, auth.reason)
                action.type == "speak" || action.type == "listen" ->
                    tasks.record(action, "retry", TaskStatus.FAILED, "Voice actions can only run from the Assistant.")
                else -> tasks.run(action, "retry")
            }
        }
    }

    fun clear() {
        tasks.clear()
        draft.update { it.copy(last = null) }
    }
}
