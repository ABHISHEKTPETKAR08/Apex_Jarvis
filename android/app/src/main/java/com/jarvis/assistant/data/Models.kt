package com.jarvis.assistant.data

data class JarvisAction(val type: String, val value: String, val params: Map<String, String> = emptyMap()) {
    val isNone: Boolean get() = type.isBlank() || type == "none"
}

data class JarvisResponse(
    val reply: String,
    val action: JarvisAction,
    val plan: PlanState? = null,
    val error: String? = null,
)

data class PlanStep(
    val id: Int,
    val tool: String,
    val summary: String,
    val status: String,
    val consequential: Boolean,
    val capability: String?,
    val message: String,
    val attempts: Int = 0,
    val errorClass: String? = null,
)

data class PlanState(
    val id: String,
    val goal: String,
    val status: String,
    val note: String,
    val done: Int,
    val total: Int,
    val steps: List<PlanStep>,
    val createdAt: Double = 0.0,
    val updatedAt: Double = 0.0,
    val finishedAt: Double? = null,
    val retryAt: Double? = null,
) {
    val isTerminal: Boolean get() = status == "completed" || status == "cancelled"
}

data class PlanDispatch(val stepId: Int, val dispatchId: String, val action: JarvisAction)

/** A step awaiting the user's decision: final confirmation, or "did it actually happen?". */
data class PlanConfirmation(val stepId: Int, val summary: String)

data class PlanUpdate(
    val plan: PlanState,
    val dispatch: PlanDispatch?,
    val confirmation: PlanConfirmation?,
    val verification: PlanConfirmation? = null,
)

/** What the phone observed for one dispatch, reported to the server. */
data class StepOutcome(
    val outcome: String, // succeeded | handed_off | failed | uncertain
    val errorClass: String? = null, // transient = nothing happened, safe to retry | permanent
    val verification: String? = null,
    val message: String = "",
)

data class CapabilityGrant(
    val id: String,
    val capability: String,
    val scopes: List<String>,
    val expiresAt: Double?,
    val targets: List<String>,
)

data class PermissionsSnapshot(val capabilities: Map<String, List<String>>, val grants: List<CapabilityGrant>)

data class AuthorizeResult(val allowed: Boolean, val reason: String)

/** Server-side integration state: NOT_CONFIGURED | NOT_CONNECTED | CONNECTING | CONNECTED | CONFIGURED. */
data class IntegrationStatus(
    val name: String,
    val state: String,
    val detail: String?,
    val extra: Map<String, String> = emptyMap(),
)

enum class TaskStatus { SUCCESS, FAILED, BLOCKED, CANCELLED } // CANCELLED = the user backed out

data class TaskRecord(
    val id: Long,
    val action: JarvisAction,
    val source: String,
    val status: TaskStatus,
    val message: String,
    val timestamp: Long,
)

data class Exchange(
    val id: Long,
    val timestamp: Long,
    val userText: String,
    val reply: String,
    val action: JarvisAction,
)

data class ChatMessage(
    val fromUser: Boolean,
    val text: String,
    val action: JarvisAction? = null,
    val task: TaskRecord? = null,
    val timestamp: Long = System.currentTimeMillis(),
)
