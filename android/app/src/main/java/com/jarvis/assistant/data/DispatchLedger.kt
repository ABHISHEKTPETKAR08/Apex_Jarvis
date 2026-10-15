package com.jarvis.assistant.data

import android.annotation.SuppressLint
import android.content.Context
import org.json.JSONObject

/**
 * Remembers the outcome of every dispatch the phone has executed, across app restarts.
 * If the server sends the same dispatch_id again (e.g. after a server restart), the stored outcome is
 * reported instead of running the action a second time.
 */
class DispatchLedger(context: Context) {
    private val prefs = context.getSharedPreferences("dispatch_ledger", Context.MODE_PRIVATE)

    @Synchronized
    fun get(dispatchId: String): StepOutcome? {
        val raw = prefs.getString(dispatchId, null) ?: return null
        return runCatching {
            val o = JSONObject(raw)
            StepOutcome(
                outcome = o.getString("outcome"),
                errorClass = o.optString("error_class").ifBlank { null },
                verification = o.optString("verification").ifBlank { null },
                message = o.optString("message"),
            )
        }.getOrNull()
    }

    @SuppressLint("ApplySharedPref") // commit() on purpose: the outcome must be durable before it is reported
    @Synchronized
    fun put(dispatchId: String, outcome: StepOutcome) {
        val o = JSONObject()
            .put("outcome", outcome.outcome)
            .put("error_class", outcome.errorClass.orEmpty())
            .put("verification", outcome.verification.orEmpty())
            .put("message", outcome.message)
            .put("t", System.currentTimeMillis())
        val editor = prefs.edit().putString(dispatchId, o.toString())
        val all = prefs.all
        if (all.size > MAX_ENTRIES) {
            all.entries
                .sortedBy { runCatching { JSONObject(it.value as String).optLong("t") }.getOrDefault(0L) }
                .take(all.size - MAX_ENTRIES)
                .forEach { editor.remove(it.key) }
        }
        editor.commit()
    }

    private companion object {
        const val MAX_ENTRIES = 300
    }
}
