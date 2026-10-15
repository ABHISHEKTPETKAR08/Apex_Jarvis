package com.jarvis.assistant.voice

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.speech.SpeechRecognizer
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.jarvis.assistant.HandsFreeStatus
import com.jarvis.assistant.JarvisApp
import com.jarvis.assistant.MainActivity
import com.jarvis.assistant.data.ActionExecutor
import com.jarvis.assistant.data.Exchange
import com.jarvis.assistant.data.JarvisAction
import com.jarvis.assistant.data.Prepared
import com.jarvis.assistant.data.TaskStatus
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * User-started hands-free mode: a microphone foreground service with a persistent notification.
 *
 * - Only runs after the user taps Start while JARVIS is on screen (Android requires that for microphone
 *   foreground services); stops on Stop, when the mic permission is gone, or when the service is destroyed.
 *   START_NOT_STICKY: never restarted silently.
 * - Uses the device's SpeechRecognizer in short sessions. Only utterances starting with "Jarvis" are sent to
 *   the server; everything else is discarded on the phone. The recognizer itself may process audio online
 *   (depends on the device's speech service) — the notification says listening is on the whole time.
 * - Android doesn't let a background app open other apps, so actions become a "tap to run" notification
 *   instead of being reported as done.
 */
class HandsFreeService : Service() {
    companion object {
        const val ACTION_START = "com.jarvis.assistant.handsfree.START"
        const val ACTION_STOP = "com.jarvis.assistant.handsfree.STOP"
        private const val CHANNEL_STATUS = "hands_free"
        private const val CHANNEL_ACTIONS = "jarvis_actions"
        private const val NOTIF_ID = 4201
        private const val MAX_ERROR_STREAK = 6

        fun start(ctx: Context) = ContextCompat.startForegroundService(
            ctx, Intent(ctx, HandsFreeService::class.java).setAction(ACTION_START),
        )

        fun stop(ctx: Context) {
            ctx.startService(Intent(ctx, HandsFreeService::class.java).setAction(ACTION_STOP))
        }
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val container get() = (application as JarvisApp).container
    private var voice: VoiceEngine? = null
    private var listenJob: Job? = null
    private var errorStreak = 0
    private var spokeAt = 0L
    private var nextActionId = NOTIF_ID + 1

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            shutdown(null)
            return START_NOT_STICKY
        }
        if (!hasMic()) {
            shutdown("Microphone permission is needed for hands-free mode.")
            return START_NOT_STICKY
        }
        createChannels()
        try {
            val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0
            ServiceCompat.startForeground(this, NOTIF_ID, statusNotification("Starting…"), type)
        } catch (e: Exception) { // e.g. ForegroundServiceStartNotAllowedException when not started from the screen
            shutdown("Android didn't allow background listening (${e.javaClass.simpleName}). Open JARVIS and tap Start.")
            return START_NOT_STICKY
        }
        if (voice == null) {
            val v = VoiceEngine(this)
            if (!v.recognitionAvailable) {
                v.release()
                shutdown("This phone has no speech recognizer available to apps.")
                return START_NOT_STICKY
            }
            v.onResult = ::onHeard
            v.onErrorCode = ::onRecognizerError
            voice = v
        }
        errorStreak = 0
        listenSoon(0)
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        listenJob?.cancel()
        voice?.release()
        voice = null
        scope.cancel()
        if (container.handsFree.value.active) container.handsFree.value = HandsFreeStatus(false, "Off")
        super.onDestroy()
    }

    // ---- listening loop ---------------------------------------------------
    private fun listenSoon(delayMs: Long) {
        listenJob?.cancel()
        listenJob = scope.launch {
            delay(delayMs)
            val v = voice ?: return@launch
            // Don't listen to our own voice: wait for any reply to finish.
            if (System.currentTimeMillis() - spokeAt < 3_000) {
                withTimeoutOrNull(3_000) { v.phase.first { it == VoicePhase.SPEAKING } }
            }
            v.phase.first { it == VoicePhase.IDLE }
            if (!hasMic()) {
                shutdown("Microphone permission was turned off, so hands-free stopped.")
                return@launch
            }
            setStatus("Listening for “Jarvis …”")
            v.startListening()
        }
    }

    private fun onHeard(text: String) {
        errorStreak = 0
        val cmd = WakeWord.command(text)
        android.util.Log.i("JarvisHandsFree", if (cmd == null) "heard speech without wake word (discarded)" else "wake word heard")
        when {
            cmd == null -> listenSoon(150) // not addressed to JARVIS: discarded, never sent anywhere
            cmd.isEmpty() -> {
                say("Yes, sir?")
                listenSoon(200)
            }
            else -> scope.launch {
                setStatus("Working on: $cmd")
                handle(cmd)
                listenSoon(300)
            }
        }
    }

    private fun onRecognizerError(code: Int) {
        android.util.Log.i("JarvisHandsFree", "recognizer error $code")
        when (code) {
            SpeechRecognizer.ERROR_NO_MATCH, SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> listenSoon(200) // silence
            SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS ->
                shutdown("Microphone permission was denied, so hands-free stopped.")
            else -> {
                errorStreak++
                if (errorStreak >= MAX_ERROR_STREAK) {
                    shutdown("The speech recognizer keeps failing (error $code), so hands-free stopped. Tap-to-speak still works.")
                } else {
                    val wait = (1_000L shl errorStreak).coerceAtMost(30_000L)
                    setStatus("Recognizer busy (error $code); retrying in ${wait / 1000}s")
                    listenSoon(wait)
                }
            }
        }
    }

    // ---- commands ---------------------------------------------------------
    private suspend fun handle(cmd: String) {
        val url = container.settings.serverUrl.value
        val r = try {
            container.client.chat(url, cmd)
        } catch (e: Exception) {
            say("I can't reach the JARVIS server, sir.")
            return
        }
        val result = when {
            r.plan != null -> {
                actionNotification("Plan needs your approval", r.plan.goal, appIntent())
                "${r.reply} Open JARVIS to approve it.".trim()
            }
            r.action.isNone -> r.reply.ifBlank { r.error ?: "" }
            else -> runAction(r.action, r.reply)
        }
        container.history.add(Exchange(System.currentTimeMillis(), System.currentTimeMillis(), cmd, result, r.action))
        say(result)
    }

    /** Returns what actually happened, in words. */
    private suspend fun runAction(action: JarvisAction, reply: String): String {
        val tasks = container.tasks
        if (container.appVisible.value) { // JARVIS on screen: run it now and report the real result
            val t = tasks.run(action, "hands-free") ?: return reply
            return if (t.status == TaskStatus.SUCCESS) reply.ifBlank { t.message } else t.message
        }
        return when (action.type) {
            "speak" -> action.value
            "listen" -> reply.ifBlank { "I'm listening, sir." }
            "camera_capture" -> {
                actionNotification("Take a photo", "Open JARVIS and ask again to use the camera.", appIntent())
                tasks.record(action, "hands-free", TaskStatus.BLOCKED, "Camera needs JARVIS on screen")
                "I can only use the camera with JARVIS on screen. I've left you a notification."
            }
            else -> when (val p = ActionExecutor.prepare(this, action, container.settings.blockedPackages.value)) {
                is Prepared.Done -> {
                    tasks.record(action, "hands-free", p.result.status, p.result.message)
                    p.result.message
                }
                is Prepared.Launch -> {
                    val pi = PendingIntent.getActivity(this, nextActionId, p.intent,
                        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
                    actionNotification("Tap to finish", p.message, pi)
                    tasks.record(action, "hands-free", TaskStatus.BLOCKED, "Waiting for your tap: ${p.message}")
                    "Android doesn't let me open that from the background. Tap the JARVIS notification to finish."
                }
            }
        }
    }

    private fun say(text: String) {
        if (text.isBlank()) return
        if (!container.settings.speakReplies.value) return
        spokeAt = System.currentTimeMillis()
        setStatus("Speaking")
        voice?.speak(text)
    }

    // ---- state / notifications ---------------------------------------------
    private fun shutdown(error: String?) {
        android.util.Log.i("JarvisHandsFree", "stopped: ${error ?: "by user"}")
        listenJob?.cancel()
        voice?.release()
        voice = null
        container.handsFree.value = HandsFreeStatus(false, "Off", error)
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun setStatus(label: String) {
        // Logs state only, never what was said.
        android.util.Log.i("JarvisHandsFree", if (label.startsWith("Working on")) "Working on a command" else label)
        container.handsFree.value = HandsFreeStatus(true, label)
        getSystemService(NotificationManager::class.java).notify(NOTIF_ID, statusNotification(label))
    }

    private fun hasMic() = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    private fun appIntent(): PendingIntent = PendingIntent.getActivity(
        this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_IMMUTABLE,
    )

    private fun statusNotification(label: String): Notification {
        val stop = PendingIntent.getService(this, 1, Intent(this, HandsFreeService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, CHANNEL_STATUS)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("JARVIS hands-free is on")
            .setContentText(label)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(appIntent())
            .addAction(0, "Stop listening", stop)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    private fun actionNotification(title: String, text: String, tap: PendingIntent) {
        val n = NotificationCompat.Builder(this, CHANNEL_ACTIONS)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(title)
            .setContentText(text)
            .setContentIntent(tap)
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .build()
        getSystemService(NotificationManager::class.java).notify(nextActionId++, n)
    }

    private fun createChannels() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL_STATUS, "Hands-free listening", NotificationManager.IMPORTANCE_LOW))
        nm.createNotificationChannel(NotificationChannel(CHANNEL_ACTIONS, "JARVIS actions", NotificationManager.IMPORTANCE_HIGH))
    }
}
