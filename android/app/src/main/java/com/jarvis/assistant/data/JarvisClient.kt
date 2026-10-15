package com.jarvis.assistant.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.UUID
import java.util.concurrent.TimeUnit

/** Server rejected a request; message is the server's explanation. */
class JarvisServerException(message: String) : Exception(message)

class JarvisClient {
    private val http = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(90, TimeUnit.SECONDS)
        .build()
    private var sessionId = UUID.randomUUID().toString()
    private val json = "application/json".toMediaType()

    fun newSession() {
        sessionId = UUID.randomUUID().toString()
    }

    suspend fun chat(baseUrl: String, text: String): JarvisResponse {
        val obj = post(baseUrl, "/chat", JSONObject().put("text", text).put("session_id", sessionId))
        return JarvisResponse(
            reply = obj.optString("reply", ""),
            action = parseAction(obj.optJSONObject("action")),
            plan = obj.optJSONObject("plan")?.let(::parsePlan),
            error = obj.optString("error").ifBlank { null },
        )
    }

    /** Returns the server's brain mode ("anthropic", "azure_openai" or "offline"). Throws if unreachable. */
    suspend fun health(baseUrl: String): String {
        val obj = get(baseUrl, "/health")
        val mode = obj.optString("mode", "unknown")
        return if (obj.optBoolean("configured", true)) mode else "$mode (not configured)"
    }

    // ---- plans ------------------------------------------------------------
    suspend fun approvePlan(baseUrl: String, planId: String) = planCall(baseUrl, "/plans/$planId/approve")
    suspend fun cancelPlan(baseUrl: String, planId: String) = planCall(baseUrl, "/plans/$planId/cancel")
    suspend fun resumePlan(baseUrl: String, planId: String) = planCall(baseUrl, "/plans/$planId/next")
    suspend fun confirmStep(baseUrl: String, planId: String, stepId: Int) =
        planCall(baseUrl, "/plans/$planId/steps/$stepId/confirm")
    suspend fun retryStep(baseUrl: String, planId: String, stepId: Int) =
        planCall(baseUrl, "/plans/$planId/steps/$stepId/retry")
    suspend fun verifyStep(baseUrl: String, planId: String, stepId: Int, happened: Boolean) =
        planCall(baseUrl, "/plans/$planId/steps/$stepId/verify", JSONObject().put("happened", happened))

    /**
     * Reports a dispatch result. Network failures are retried with bounded exponential backoff and the same
     * Idempotency-Key; the server also ignores repeats for the same dispatch_id, so a retry can't double-apply.
     */
    suspend fun reportStep(baseUrl: String, planId: String, stepId: Int, dispatchId: String, outcome: StepOutcome): PlanUpdate {
        val body = JSONObject()
            .put("dispatch_id", dispatchId)
            .put("outcome", outcome.outcome)
            .put("message", outcome.message)
        outcome.errorClass?.let { body.put("error_class", it) }
        outcome.verification?.let { body.put("verification", it) }
        val path = "/plans/$planId/steps/$stepId/result"
        val key = "result-$dispatchId"
        var delayMs = 1_000L
        repeat(REPORT_ATTEMPTS - 1) {
            try {
                return planCall(baseUrl, path, body, key)
            } catch (e: IOException) {
                delay(delayMs)
                delayMs *= 2
            }
        }
        return planCall(baseUrl, path, body, key)
    }

    suspend fun tasks(baseUrl: String, limit: Int = 30): List<PlanState> {
        val arr = get(baseUrl, "/tasks?limit=$limit").getJSONArray("tasks")
        return List(arr.length()) { parsePlan(arr.getJSONObject(it)) }
    }

    private suspend fun planCall(
        baseUrl: String,
        path: String,
        body: JSONObject = JSONObject(),
        idempotencyKey: String = UUID.randomUUID().toString(),
    ): PlanUpdate {
        val obj = post(baseUrl, path, body, idempotencyKey)
        fun prompt(name: String) =
            obj.optJSONObject(name)?.let { PlanConfirmation(it.getInt("step_id"), it.optString("summary")) }
        return PlanUpdate(
            plan = parsePlan(obj.getJSONObject("plan")),
            dispatch = obj.optJSONObject("dispatch")?.let {
                PlanDispatch(it.getInt("step_id"), it.optString("dispatch_id"), parseAction(it.optJSONObject("action")))
            },
            confirmation = prompt("confirmation"),
            verification = prompt("verification"),
        )
    }

    // ---- permissions ------------------------------------------------------
    suspend fun permissions(baseUrl: String): PermissionsSnapshot {
        val obj = get(baseUrl, "/permissions")
        val caps = obj.getJSONObject("capabilities")
        return PermissionsSnapshot(
            capabilities = caps.keys().asSequence().associateWith { strings(caps.getJSONArray(it)) },
            grants = obj.getJSONArray("grants").let { arr ->
                List(arr.length()) { i ->
                    val g = arr.getJSONObject(i)
                    CapabilityGrant(
                        id = g.getString("id"),
                        capability = g.getString("capability"),
                        scopes = strings(g.optJSONArray("scopes")),
                        expiresAt = if (g.isNull("expires_at")) null else g.getDouble("expires_at"),
                        targets = strings(g.optJSONArray("targets")),
                    )
                }
            },
        )
    }

    suspend fun grant(baseUrl: String, capability: String, scopes: List<String>, ttlSeconds: Long?) {
        val body = JSONObject().put("capability", capability).put("scopes", JSONArray(scopes))
        if (ttlSeconds != null) body.put("ttl_seconds", ttlSeconds)
        post(baseUrl, "/permissions/grant", body, UUID.randomUUID().toString())
    }

    suspend fun revoke(baseUrl: String, grantId: String) {
        post(baseUrl, "/permissions/revoke", JSONObject().put("grant_id", grantId), UUID.randomUUID().toString())
    }

    suspend fun integrations(baseUrl: String): List<IntegrationStatus> {
        val obj = get(baseUrl, "/integrations/status")
        return obj.keys().asSequence().mapNotNull { key ->
            val o = obj.optJSONObject(key) ?: return@mapNotNull null
            IntegrationStatus(
                name = key,
                state = o.optString("state", "UNKNOWN"),
                detail = o.optString("detail").ifBlank { null },
                extra = o.keys().asSequence().filter { it != "state" && it != "detail" }
                    .associateWith { o.optString(it) },
            )
        }.toList()
    }

    suspend fun connectGraph(baseUrl: String) {
        post(baseUrl, "/integrations/graph/connect", JSONObject())
    }

    suspend fun disconnectGraph(baseUrl: String) {
        post(baseUrl, "/integrations/graph/disconnect", JSONObject())
    }

    /** Azure neural TTS through the server; returns MP3 bytes. Throws JarvisServerException (e.g. NOT_CONFIGURED). */
    suspend fun tts(baseUrl: String, text: String): ByteArray = withContext(Dispatchers.IO) {
        val req = Request.Builder().url(baseUrl.trimEnd('/') + "/speech/tts")
            .post(JSONObject().put("text", text).toString().toRequestBody(json)).build()
        http.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) {
                val o = runCatching { JSONObject(resp.body?.string().orEmpty()) }.getOrNull()
                throw JarvisServerException(o?.let { "${it.optString("code")}: ${it.optString("detail")}" } ?: "HTTP ${resp.code}")
            }
            resp.body?.bytes() ?: ByteArray(0)
        }
    }

    suspend fun authorizeAction(baseUrl: String, action: JarvisAction): AuthorizeResult {
        val obj = post(baseUrl, "/actions/authorize", JSONObject().put("type", action.type).put("value", action.value))
        return AuthorizeResult(obj.optBoolean("allowed", false), obj.optString("reason"))
    }

    // ---- HTTP -------------------------------------------------------------
    private suspend fun get(baseUrl: String, path: String): JSONObject =
        send(Request.Builder().url(baseUrl.trimEnd('/') + path).get().build())

    private suspend fun post(baseUrl: String, path: String, body: JSONObject, idempotencyKey: String? = null): JSONObject {
        val req = Request.Builder().url(baseUrl.trimEnd('/') + path).post(body.toString().toRequestBody(json))
        idempotencyKey?.let { req.header("Idempotency-Key", it) }
        return send(req.build())
    }

    private suspend fun send(req: Request): JSONObject = withContext(Dispatchers.IO) {
        http.newCall(req).execute().use { resp ->
            val text = resp.body?.string().orEmpty()
            if (!resp.isSuccessful) {
                val detail = runCatching {
                    val d = JSONObject(text).opt("detail")
                    if (d is JSONObject) "${d.optString("code")}: ${d.optString("detail")}" else d?.toString()
                }.getOrNull().orEmpty()
                throw JarvisServerException(detail.ifBlank { "Server returned HTTP ${resp.code}" })
            }
            JSONObject(text)
        }
    }

    private fun parseAction(obj: JSONObject?): JarvisAction {
        if (obj == null) return JarvisAction("none", "")
        val params = obj.optJSONObject("params")
        return JarvisAction(
            type = obj.optString("type", "none"),
            value = obj.optString("value", ""),
            params = params?.keys()?.asSequence()?.associateWith { params.optString(it) } ?: emptyMap(),
        )
    }

    private fun parsePlan(obj: JSONObject): PlanState {
        val steps = obj.getJSONArray("steps")
        val progress = obj.optJSONObject("progress")
        return PlanState(
            id = obj.getString("id"),
            goal = obj.optString("goal"),
            status = obj.optString("status"),
            note = obj.optString("note"),
            done = progress?.optInt("done") ?: 0,
            total = progress?.optInt("total") ?: steps.length(),
            steps = List(steps.length()) { i ->
                val s = steps.getJSONObject(i)
                PlanStep(
                    id = s.getInt("id"),
                    tool = s.optString("tool"),
                    summary = s.optString("summary"),
                    status = s.optString("status"),
                    consequential = s.optBoolean("consequential"),
                    capability = s.optString("capability").takeUnless { s.isNull("capability") || it.isBlank() },
                    message = s.optString("message"),
                    attempts = s.optInt("attempts"),
                    errorClass = s.optString("error_class").takeUnless { s.isNull("error_class") || it.isBlank() },
                )
            },
            createdAt = obj.optDouble("created_at", 0.0),
            updatedAt = obj.optDouble("updated_at", 0.0),
            finishedAt = if (obj.isNull("finished_at")) null else obj.optDouble("finished_at"),
            retryAt = if (obj.isNull("retry_at")) null else obj.optDouble("retry_at"),
        )
    }

    private companion object {
        const val REPORT_ATTEMPTS = 3
    }

    private fun strings(arr: JSONArray?): List<String> =
        if (arr == null) emptyList() else List(arr.length()) { arr.getString(it) }
}
