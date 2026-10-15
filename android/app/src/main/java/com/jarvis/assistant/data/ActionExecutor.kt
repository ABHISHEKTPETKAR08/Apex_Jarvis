package com.jarvis.assistant.data

import android.Manifest
import android.app.SearchManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.AlarmClock
import android.provider.CalendarContract
import android.provider.ContactsContract
import android.provider.MediaStore
import java.text.SimpleDateFormat
import java.util.Locale

data class ActionResult(val status: TaskStatus, val message: String)

/** An action turned into an Intent (not started yet), or a final result when it can't run. */
sealed interface Prepared {
    data class Launch(val intent: Intent, val message: String) : Prepared
    data class Done(val result: ActionResult) : Prepared
}

/** Executes a server-requested action on the device. */
object ActionExecutor {
    val SUPPORTED = listOf("open_app", "open_url", "web_search", "dial", "set_alarm", "set_timer")

    /** Actions gated by a server capability; re-running them outside a plan needs server authorization. */
    val CAPABILITY_TYPES = setOf(
        "compose_email", "whatsapp_message", "camera_capture", "calendar_insert", "calendar_view", "speak", "listen",
    )

    fun label(type: String): String = when (type) {
        "open_app" -> "Open app"
        "open_url" -> "Open website"
        "web_search" -> "Web search"
        "dial" -> "Call"
        "set_alarm" -> "Set alarm"
        "set_timer" -> "Set timer"
        "compose_email" -> "Email"
        "whatsapp_message" -> "WhatsApp"
        "camera_capture" -> "Camera"
        "calendar_insert" -> "Add event"
        "calendar_view" -> "Calendar"
        "speak" -> "Speak"
        "share_text" -> "Share"
        "listen" -> "Listen"
        else -> type
    }

    fun hint(type: String): String = when (type) {
        "open_app" -> "App name, e.g. YouTube"
        "open_url" -> "URL, e.g. github.com"
        "web_search" -> "Search query"
        "dial" -> "Number or contact name"
        "set_alarm" -> "Time HH:MM (24h)"
        "set_timer" -> "Seconds, e.g. 300"
        else -> "Value"
    }

    /** Builds the Intent for an action without starting it. */
    fun prepare(ctx: Context, action: JarvisAction, blocked: Set<String>): Prepared {
        val v = action.value.trim()
        var okMessage = "${label(action.type)}: $v"
        val intent: Intent = when (action.type) {
            "open_url" -> Intent(Intent.ACTION_VIEW, Uri.parse(if (v.startsWith("http")) v else "https://$v"))
            "web_search" -> Intent(Intent.ACTION_WEB_SEARCH).putExtra(SearchManager.QUERY, v)
                .takeIf { it.resolveActivity(ctx.packageManager) != null }
                ?: Intent(Intent.ACTION_VIEW, Uri.parse("https://www.google.com/search?q=" + Uri.encode(v)))
            "dial" -> Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + (resolveNumber(ctx, v) ?: "")))
            "open_app" -> when (val d = AppResolver.decide(v, installedApps(ctx), blocked)) {
                is LaunchDecision.NotInstalled -> return failP("I couldn't find an installed app called ${d.query}.")
                is LaunchDecision.Blocked ->
                    return Prepared.Done(ActionResult(TaskStatus.BLOCKED, "Access to ${d.app.label} is disabled in App Access."))
                is LaunchDecision.Launch -> {
                    okMessage = "Opened ${d.app.label}"
                    ctx.packageManager.getLaunchIntentForPackage(d.app.packageName)
                        ?: return failP("${d.app.label} can't be launched.")
                }
            }
            "set_alarm" -> {
                val parts = v.split(":")
                val h = parts.getOrNull(0)?.toIntOrNull()?.takeIf { it in 0..23 } ?: return failP("Invalid alarm time.")
                val m = parts.getOrNull(1)?.toIntOrNull()?.takeIf { it in 0..59 } ?: 0
                Intent(AlarmClock.ACTION_SET_ALARM)
                    .putExtra(AlarmClock.EXTRA_HOUR, h)
                    .putExtra(AlarmClock.EXTRA_MINUTES, m)
                    .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
            }
            "set_timer" -> Intent(AlarmClock.ACTION_SET_TIMER)
                .putExtra(AlarmClock.EXTRA_LENGTH, v.toIntOrNull()?.takeIf { it > 0 } ?: return failP("Invalid timer length."))
                .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
            "compose_email" -> Intent(Intent.ACTION_SENDTO, Uri.parse("mailto:"))
                .putExtra(Intent.EXTRA_EMAIL, arrayOf(action.params["to"] ?: v))
                .putExtra(Intent.EXTRA_SUBJECT, action.params["subject"].orEmpty())
                .putExtra(Intent.EXTRA_TEXT, action.params["body"].orEmpty())
            "whatsapp_message" -> {
                val number = resolveNumber(ctx, action.params["to"] ?: v)?.filter { it.isDigit() }
                    ?: return failP("I couldn't find a phone number for ${action.params["to"] ?: v}.")
                Intent(Intent.ACTION_VIEW, Uri.parse("https://wa.me/$number?text=" + Uri.encode(action.params["message"].orEmpty())))
            }
            "camera_capture" -> Intent(MediaStore.INTENT_ACTION_STILL_IMAGE_CAMERA)
            "calendar_insert" -> {
                val start = runCatching {
                    SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.US).parse(action.params["start"].orEmpty())!!.time
                }.getOrNull() ?: return failP("Invalid event start time.")
                val minutes = action.params["duration_minutes"]?.toLongOrNull() ?: 60L
                Intent(Intent.ACTION_INSERT, CalendarContract.Events.CONTENT_URI)
                    .putExtra(CalendarContract.Events.TITLE, action.params["title"] ?: v)
                    .putExtra(CalendarContract.EXTRA_EVENT_BEGIN_TIME, start)
                    .putExtra(CalendarContract.EXTRA_EVENT_END_TIME, start + minutes * 60_000)
            }
            "calendar_view" -> {
                val at = runCatching { SimpleDateFormat("yyyy-MM-dd", Locale.US).parse(v)!!.time }
                    .getOrDefault(System.currentTimeMillis())
                Intent(Intent.ACTION_VIEW, CalendarContract.CONTENT_URI.buildUpon().appendPath("time").appendPath(at.toString()).build())
            }
            "share_text" -> Intent.createChooser(
                Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, v),
                "Share with",
            )
            else -> return failP("Unknown action ${action.type}.")
        }
        return Prepared.Launch(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK), okMessage)
    }

    /** Starts the action now. Only valid while JARVIS is on screen (Android blocks background activity starts). */
    fun execute(ctx: Context, action: JarvisAction, blocked: Set<String>): ActionResult =
        when (val p = prepare(ctx, action, blocked)) {
            is Prepared.Done -> p.result
            is Prepared.Launch -> try {
                ctx.startActivity(p.intent)
                ActionResult(TaskStatus.SUCCESS, p.message)
            } catch (e: Exception) {
                fail("I couldn't do that: ${e.message}")
            }
        }

    private fun fail(msg: String) = ActionResult(TaskStatus.FAILED, msg)

    private fun failP(msg: String) = Prepared.Done(fail(msg))

    fun installedApps(ctx: Context): List<AppEntry> {
        val pm = ctx.packageManager
        val main = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        return pm.queryIntentActivities(main, 0)
            .map { AppEntry(it.loadLabel(pm).toString(), it.activityInfo.packageName) }
            .distinctBy { it.packageName }
    }

    private fun resolveNumber(ctx: Context, who: String): String? {
        if (who.any { it.isDigit() }) return who.filter { it.isDigit() || it == '+' }
        if (ctx.checkSelfPermission(Manifest.permission.READ_CONTACTS) != PackageManager.PERMISSION_GRANTED) return null
        ctx.contentResolver.query(
            ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
            arrayOf(ContactsContract.CommonDataKinds.Phone.NUMBER),
            "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} LIKE ?",
            arrayOf("%$who%"),
            null,
        )?.use { c -> if (c.moveToFirst()) return c.getString(0) }
        return null
    }
}
