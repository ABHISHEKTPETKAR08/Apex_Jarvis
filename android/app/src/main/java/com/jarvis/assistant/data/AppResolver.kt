package com.jarvis.assistant.data

/** A launchable app on the device. */
data class AppEntry(val label: String, val packageName: String)

sealed interface LaunchDecision {
    data class Launch(val app: AppEntry) : LaunchDecision
    data class Blocked(val app: AppEntry) : LaunchDecision
    data class NotInstalled(val query: String) : LaunchDecision
}

/** Maps a spoken/typed app name to an installed app. Pure Kotlin so it is unit-tested without a device. */
object AppResolver {
    /** Common spoken names -> package names, in order of preference. */
    private val ALIASES: Map<String, List<String>> = mapOf(
        "youtube" to listOf("com.google.android.youtube"),
        "yt" to listOf("com.google.android.youtube"),
        "youtubemusic" to listOf("com.google.android.apps.youtube.music"),
        "ytmusic" to listOf("com.google.android.apps.youtube.music"),
        "amongus" to listOf("com.innersloth.spacemafia"),
        "whatsapp" to listOf("com.whatsapp", "com.whatsapp.w4b"),
        "instagram" to listOf("com.instagram.android"),
        "insta" to listOf("com.instagram.android"),
        "gmail" to listOf("com.google.android.gm"),
        "maps" to listOf("com.google.android.apps.maps"),
        "googlemaps" to listOf("com.google.android.apps.maps"),
        "chrome" to listOf("com.android.chrome"),
        "playstore" to listOf("com.android.vending"),
        "spotify" to listOf("com.spotify.music"),
        "camera" to listOf("com.android.camera", "com.google.android.GoogleCamera", "com.sec.android.app.camera"),
        "settings" to listOf("com.android.settings"),
        "calculator" to listOf("com.miui.calculator", "com.google.android.calculator"),
    )

    private val FILLER = Regex("\\b(the|app|application)\\b", RegexOption.IGNORE_CASE)

    fun normalize(s: String): String = s.lowercase().filter { it.isLetterOrDigit() }

    fun resolve(query: String, apps: List<AppEntry>): AppEntry? {
        val raw = query.trim()
        if (raw.isEmpty()) return null
        apps.firstOrNull { it.packageName.equals(raw, ignoreCase = true) }?.let { return it }
        val n = normalize(FILLER.replace(raw, " ")).ifEmpty { normalize(raw) }
        if (n.isEmpty()) return null
        apps.firstOrNull { normalize(it.label) == n }?.let { return it }
        ALIASES[n]?.firstNotNullOfOrNull { pkg -> apps.firstOrNull { it.packageName == pkg } }?.let { return it }
        if (n.length < 3) return null // too short to guess safely
        apps.filter { normalize(it.label).startsWith(n) }.minByOrNull { it.label.length }?.let { return it }
        return apps.filter { normalize(it.label).contains(n) }.minByOrNull { it.label.length }
    }

    fun decide(query: String, apps: List<AppEntry>, blocked: Set<String>): LaunchDecision {
        val app = resolve(query, apps) ?: return LaunchDecision.NotInstalled(query.trim())
        return if (app.packageName in blocked) LaunchDecision.Blocked(app) else LaunchDecision.Launch(app)
    }
}
