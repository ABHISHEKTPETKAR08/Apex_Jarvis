package com.jarvis.assistant.data

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

class SettingsRepository(context: Context) {
    private val prefs = context.getSharedPreferences("jarvis", Context.MODE_PRIVATE)

    private val _serverUrl = MutableStateFlow(prefs.getString(KEY_SERVER, DEFAULT_SERVER) ?: DEFAULT_SERVER)
    val serverUrl: StateFlow<String> = _serverUrl.asStateFlow()

    private val _blocked = MutableStateFlow(prefs.getStringSet(KEY_BLOCKED, emptySet())?.toSet() ?: emptySet())
    val blockedPackages: StateFlow<Set<String>> = _blocked.asStateFlow()

    private val _speakReplies = MutableStateFlow(prefs.getBoolean(KEY_SPEAK, true))
    val speakReplies: StateFlow<Boolean> = _speakReplies.asStateFlow()

    /** Use the server's Azure neural voice for replies (falls back to the phone's voice if unavailable). */
    private val _azureVoice = MutableStateFlow(prefs.getBoolean(KEY_AZURE_VOICE, false))
    val azureVoice: StateFlow<Boolean> = _azureVoice.asStateFlow()

    fun setAzureVoice(enabled: Boolean) {
        _azureVoice.value = enabled
        prefs.edit().putBoolean(KEY_AZURE_VOICE, enabled).apply()
    }

    fun setServerUrl(url: String) {
        _serverUrl.value = url.trim()
        prefs.edit().putString(KEY_SERVER, url.trim()).apply()
    }

    fun setAppAllowed(packageName: String, allowed: Boolean) {
        val next = if (allowed) _blocked.value - packageName else _blocked.value + packageName
        setBlocked(next)
    }

    fun setBlocked(packages: Set<String>) {
        _blocked.value = packages
        prefs.edit().putStringSet(KEY_BLOCKED, packages).apply()
    }

    fun setSpeakReplies(enabled: Boolean) {
        _speakReplies.value = enabled
        prefs.edit().putBoolean(KEY_SPEAK, enabled).apply()
    }

    companion object {
        // Works over USB with `adb reverse tcp:8000 tcp:8000`; change in App Access for Wi-Fi.
        const val DEFAULT_SERVER = "http://127.0.0.1:8000"
        private const val KEY_SERVER = "server"
        private const val KEY_BLOCKED = "blocked_packages"
        private const val KEY_SPEAK = "speak_replies"
        private const val KEY_AZURE_VOICE = "azure_voice"
    }
}
