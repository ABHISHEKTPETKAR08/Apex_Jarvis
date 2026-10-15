package com.jarvis.assistant.ui.assistant

import android.Manifest
import android.app.Application
import android.content.pm.PackageManager
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.jarvis.assistant.container
import com.jarvis.assistant.data.ChatMessage
import com.jarvis.assistant.data.Exchange
import com.jarvis.assistant.data.JarvisAction
import com.jarvis.assistant.data.PlanConfirmation
import com.jarvis.assistant.data.PlanState
import com.jarvis.assistant.data.PlanUpdate
import com.jarvis.assistant.data.StepOutcome
import com.jarvis.assistant.data.TaskRecord
import com.jarvis.assistant.data.TaskStatus
import com.jarvis.assistant.voice.VoiceEngine
import com.jarvis.assistant.voice.VoicePhase
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class AssistantUiState(
    val messages: List<ChatMessage> = emptyList(),
    val phase: VoicePhase = VoicePhase.IDLE,
    val level: Float = 0f,
    val partial: String = "",
    val notice: String? = null,
    val serverUrl: String = "",
    val voiceAvailable: Boolean = true,
    val plan: PlanState? = null,
    val confirmation: PlanConfirmation? = null,
    val verification: PlanConfirmation? = null,
    val planBusy: Boolean = false,
)

private data class LocalState(
    val messages: List<ChatMessage>,
    val thinking: Boolean = false,
    val notice: String? = null,
    val plan: PlanState? = null,
    val confirmation: PlanConfirmation? = null,
    val verification: PlanConfirmation? = null,
    val planBusy: Boolean = false,
)

class AssistantViewModel(app: Application) : AndroidViewModel(app) {
    private val container = app.container
    private val voice = VoiceEngine(app)

    private val local = MutableStateFlow(LocalState(listOf(greeting())))

    val uiState: StateFlow<AssistantUiState> = combine(
        local, voice.phase, voice.level, voice.partial, container.settings.serverUrl,
    ) { l, phase, level, partial, url ->
        AssistantUiState(
            messages = l.messages,
            phase = if (l.thinking) VoicePhase.THINKING else phase,
            level = level,
            partial = partial,
            notice = l.notice,
            serverUrl = url,
            voiceAvailable = voice.recognitionAvailable,
            plan = l.plan,
            confirmation = l.confirmation,
            verification = l.verification,
            planBusy = l.planBusy,
        )
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), AssistantUiState(messages = local.value.messages))

    private val url get() = container.settings.serverUrl.value
    val handsFree = container.handsFree
    private var retryJob: Job? = null
    private var onScreen = false

    init {
        viewModelScope.launch {
            com.jarvis.assistant.DebugCommands.pending.collect { cmd ->
                if (cmd != null) {
                    com.jarvis.assistant.DebugCommands.pending.value = null
                    send(cmd)
                }
            }
        }
        voice.onResult = { send(it) }
        voice.onError = { msg -> local.update { it.copy(notice = msg) } }
    }

    fun onMicClick() {
        when (uiState.value.phase) {
            VoicePhase.LISTENING -> voice.stopListening()
            VoicePhase.SPEAKING -> voice.stopSpeaking()
            VoicePhase.THINKING -> Unit
            VoicePhase.IDLE -> {
                if (container.handsFree.value.active) {
                    local.update { it.copy(notice = "Hands-free is listening. Say \"Jarvis\" and then your command.") }
                    return
                }
                local.update { it.copy(notice = null) }
                voice.startListening()
            }
        }
    }

    fun dismissNotice() = local.update { it.copy(notice = null) }

    fun showNotice(text: String) = local.update { it.copy(notice = text) }

    fun onForeground() {
        onScreen = true
    }

    /** No listening while JARVIS isn't visible. */
    fun onBackground() {
        onScreen = false
        voice.cancelListening()
    }

    fun send(text: String) {
        val t = text.trim()
        if (t.isEmpty() || local.value.thinking) return
        local.update { it.copy(messages = it.messages + ChatMessage(true, t), thinking = true, notice = null) }
        viewModelScope.launch {
            try {
                val r = container.client.chat(url, t)
                val task = if (r.plan == null) runAction(r.action, "voice") else null
                val reply = when {
                    task != null && task.status != TaskStatus.SUCCESS -> task.message
                    else -> r.reply.trim().ifEmpty { if (r.plan != null) "Here's the plan, sir." else task?.message.orEmpty() }
                }
                local.update {
                    it.copy(
                        messages = it.messages + ChatMessage(false, reply, r.action.takeUnless { a -> a.isNone }, task),
                        thinking = false,
                        notice = r.error,
                        plan = r.plan ?: it.plan,
                        confirmation = if (r.plan != null) null else it.confirmation,
                    )
                }
                container.history.add(Exchange(System.currentTimeMillis(), System.currentTimeMillis(), t, reply, r.action))
                speakIfEnabled(reply)
            } catch (e: Exception) {
                local.update {
                    it.copy(
                        messages = it.messages + ChatMessage(false, "I can't reach the server at $url, sir."),
                        thinking = false,
                        notice = e.message ?: e.javaClass.simpleName,
                    )
                }
            }
        }
    }

    // ---- plans ------------------------------------------------------------
    fun approvePlan() = planOp { container.client.approvePlan(url, it.id) }

    fun resumePlan() = planOp { container.client.resumePlan(url, it.id) }

    fun cancelPlan() {
        retryJob?.cancel()
        planOp { container.client.cancelPlan(url, it.id) }
    }

    fun retryStep(stepId: Int) = planOp { container.client.retryStep(url, it.id, stepId) }

    /** Explicit final confirmation from the user for the step shown in the dialog. */
    fun confirmStep() {
        val c = local.value.confirmation ?: return
        local.update { it.copy(confirmation = null) }
        planOp { container.client.confirmStep(url, it.id, c.stepId) }
    }

    /** Declining the confirmation cancels the plan; nothing is sent. */
    fun declineStep() {
        local.update { it.copy(confirmation = null) }
        cancelPlan()
    }

    /** The user's answer to "did it actually happen?" for a hand-off or an uncertain step. */
    fun verifyStep(happened: Boolean) {
        val v = local.value.verification ?: return
        local.update { it.copy(verification = null) }
        planOp { container.client.verifyStep(url, it.id, v.stepId, happened) }
    }

    fun dismissPlan() {
        retryJob?.cancel()
        local.update { it.copy(plan = null, confirmation = null, verification = null) }
    }

    private fun planOp(call: suspend (PlanState) -> PlanUpdate) {
        val plan = local.value.plan ?: return
        if (local.value.planBusy) return
        local.update { it.copy(planBusy = true, notice = null) }
        viewModelScope.launch {
            try {
                drive(call(plan))
            } catch (e: Exception) {
                local.update { it.copy(notice = e.message ?: e.javaClass.simpleName) }
            } finally {
                local.update { it.copy(planBusy = false) }
            }
        }
    }

    /**
     * Runs dispatched steps and reports each outcome until the server pauses or finishes the plan.
     * Each dispatch_id runs at most once: a repeated id is answered from the ledger instead of re-running.
     */
    private suspend fun drive(first: PlanUpdate) {
        var update = first
        while (true) {
            local.update { it.copy(plan = update.plan, confirmation = update.confirmation, verification = update.verification) }
            val dispatch = update.dispatch ?: break
            val outcome = container.ledger.get(dispatch.dispatchId)
                ?: runStep(dispatch.action).also { container.ledger.put(dispatch.dispatchId, it) }
            update = container.client.reportStep(url, update.plan.id, dispatch.stepId, dispatch.dispatchId, outcome)
        }
        when (update.plan.status) {
            "completed" -> speakIfEnabled("All done, sir.")
            "blocked" -> speakIfEnabled("I need your permission for that step, sir.")
            "step_failed" -> speakIfEnabled("A step failed, sir.")
            "awaiting_verification" -> speakIfEnabled("Please confirm whether that went through, sir.")
            "retry_scheduled" -> scheduleRetry(update.plan)
        }
    }

    /** Resumes after the server's backoff. Timing uses server timestamps, so phone clock skew doesn't matter. */
    private fun scheduleRetry(plan: PlanState) {
        val retryAt = plan.retryAt ?: return
        val waitMs = ((retryAt - plan.updatedAt) * 1000).toLong().coerceIn(0L, 60_000L) + 250L
        retryJob?.cancel()
        retryJob = viewModelScope.launch {
            delay(waitMs)
            val current = local.value.plan
            if (current?.id == plan.id && current.status == "retry_scheduled") resumePlan()
        }
    }

    /** Executes one plan step and classifies what the phone could observe. */
    private suspend fun runStep(action: JarvisAction): StepOutcome {
        val task = runAction(action, "plan", log = false)
        return when {
            task == null -> StepOutcome("succeeded", verification = "no_op")
            task.status == TaskStatus.SUCCESS -> StepOutcome(
                outcome = "succeeded",
                verification = when (action.type) {
                    "speak" -> "tts_started"
                    "listen" -> "recognizer_started"
                    "camera_capture" -> "photo_saved"
                    else -> "activity_launched" // server asks the user to verify sends and calendar events
                },
                message = task.message,
            )
            task.status == TaskStatus.CANCELLED -> StepOutcome("cancelled", message = task.message)
            // startActivity failed, input was invalid or access is blocked: the action did not happen.
            else -> StepOutcome("failed", errorClass = "permanent", message = task.message)
        }
    }

    /** Executes a phone action. Voice actions are handled here; everything else by the task executor. */
    private suspend fun runAction(action: JarvisAction, source: String, log: Boolean = true): TaskRecord? = when {
        action.isNone -> null
        action.type == "speak" -> {
            voice.speak(action.value)
            voiceRecord(action, source, "Spoke: ${action.value}", log)
        }
        action.type == "listen" -> when {
            // Only visible, user-facing listening: never in the background or without permission.
            !onScreen -> voiceRecord(action, source, "Not listening: JARVIS isn't on screen", log, TaskStatus.FAILED)
            getApplication<Application>().checkSelfPermission(Manifest.permission.RECORD_AUDIO) !=
                PackageManager.PERMISSION_GRANTED ->
                voiceRecord(action, source, "Microphone permission not granted", log, TaskStatus.FAILED)
            else -> {
                voice.startListening()
                voiceRecord(action, source, "Listening", log)
            }
        }
        else -> container.tasks.run(action, source, log)
    }

    private fun voiceRecord(
        action: JarvisAction,
        source: String,
        message: String,
        log: Boolean,
        status: TaskStatus = TaskStatus.SUCCESS,
    ): TaskRecord =
        if (log) container.tasks.record(action, source, status, message)
        else TaskRecord(0, action, source, status, message, System.currentTimeMillis())

    private fun speakIfEnabled(text: String) {
        if (!container.settings.speakReplies.value || text.isEmpty()) return
        if (!container.settings.azureVoice.value) {
            voice.speak(text)
            return
        }
        viewModelScope.launch {
            try {
                val audio = container.client.tts(url, text)
                val file = java.io.File(getApplication<Application>().cacheDir, "reply.mp3")
                kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.IO) { file.writeBytes(audio) }
                voice.playAudio(file) { voice.speak(text) }
            } catch (e: Exception) {
                // Visible fallback: native voice keeps working without Azure.
                local.update { it.copy(notice = "Azure voice unavailable (${e.message}); using the phone's voice.") }
                voice.speak(text)
            }
        }
    }

    fun newConversation() {
        container.client.newSession()
        voice.stopSpeaking()
        local.value = LocalState(listOf(greeting()))
    }

    override fun onCleared() {
        voice.release()
    }

    private fun greeting() = ChatMessage(false, "Good day, sir. Tap the core or type a command.")
}
