package com.jarvis.assistant.ui.apps

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.jarvis.assistant.container
import com.jarvis.assistant.data.CapabilityGrant
import com.jarvis.assistant.data.InstalledApp
import com.jarvis.assistant.data.IntegrationStatus
import com.jarvis.assistant.data.loadInstalledApps
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

sealed interface ServerStatus {
    data object Unknown : ServerStatus
    data object Checking : ServerStatus
    data class Online(val mode: String) : ServerStatus
    data class Offline(val reason: String) : ServerStatus
}

data class AppRow(val app: InstalledApp, val allowed: Boolean)

data class CapabilityRow(val name: String, val scopes: List<String>, val grants: List<CapabilityGrant>)

data class CapabilitiesState(
    val loading: Boolean = true,
    val rows: List<CapabilityRow> = emptyList(),
    val error: String? = null,
)

data class IntegrationsState(val items: List<IntegrationStatus> = emptyList(), val error: String? = null)

data class AppAccessUiState(
    val loading: Boolean = true,
    val apps: List<AppRow> = emptyList(),
    val allowedCount: Int = 0,
    val totalCount: Int = 0,
    val query: String = "",
    val serverUrl: String = "",
    val serverStatus: ServerStatus = ServerStatus.Unknown,
    val speakReplies: Boolean = true,
    val capabilities: CapabilitiesState = CapabilitiesState(),
    val integrations: IntegrationsState = IntegrationsState(),
    val azureVoice: Boolean = false,
)

class AppAccessViewModel(app: Application) : AndroidViewModel(app) {
    private val settings = app.container.settings
    private val client = app.container.client

    private val installed = MutableStateFlow<List<InstalledApp>?>(null)
    private val query = MutableStateFlow("")
    private val serverStatus = MutableStateFlow<ServerStatus>(ServerStatus.Unknown)
    private val capabilities = MutableStateFlow(CapabilitiesState())
    private val integrations = MutableStateFlow(IntegrationsState())
    private var pollJob: kotlinx.coroutines.Job? = null

    private val connection = combine(settings.serverUrl, serverStatus, settings.speakReplies) { url, status, speak ->
        Triple(url, status, speak)
    }
    private val serverSide = combine(capabilities, integrations, settings.azureVoice) { c, i, v -> Triple(c, i, v) }

    val uiState: StateFlow<AppAccessUiState> = combine(
        installed, query, settings.blockedPackages, connection, serverSide,
    ) { apps, q, blocked, (url, status, speak), (caps, ints, azureVoice) ->
        val all = apps.orEmpty()
        val rows = all
            .filter { q.isBlank() || it.label.contains(q, ignoreCase = true) }
            .map { AppRow(it, it.packageName !in blocked) }
        AppAccessUiState(
            loading = apps == null,
            apps = rows,
            allowedCount = all.count { it.packageName !in blocked },
            totalCount = all.size,
            query = q,
            serverUrl = url,
            serverStatus = status,
            speakReplies = speak,
            capabilities = caps,
            integrations = ints,
            azureVoice = azureVoice,
        )
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), AppAccessUiState())

    init {
        viewModelScope.launch {
            installed.value = withContext(Dispatchers.IO) { loadInstalledApps(getApplication()) }
        }
        checkServer()
        refreshCapabilities()
        refreshIntegrations()
    }

    fun refreshIntegrations() {
        viewModelScope.launch {
            integrations.value = try {
                IntegrationsState(client.integrations(settings.serverUrl.value))
            } catch (e: Exception) {
                IntegrationsState(error = e.message ?: e.javaClass.simpleName)
            }
        }
    }

    /** Starts Microsoft sign-in, then polls until the server reports the account connected (max 15 min). */
    fun connectGraph() {
        pollJob?.cancel()
        pollJob = viewModelScope.launch {
            try {
                client.connectGraph(settings.serverUrl.value)
            } catch (e: Exception) {
                integrations.update { it.copy(error = e.message ?: e.javaClass.simpleName) }
                return@launch
            }
            repeat(300) {
                refreshIntegrationsNow()
                val graph = integrations.value.items.firstOrNull { it.name == "microsoft_graph" }
                if (graph != null && graph.state != "CONNECTING") return@launch
                kotlinx.coroutines.delay(3_000)
            }
        }
    }

    fun disconnectGraph() {
        viewModelScope.launch {
            try {
                client.disconnectGraph(settings.serverUrl.value)
            } catch (e: Exception) {
                integrations.update { it.copy(error = e.message ?: e.javaClass.simpleName) }
            }
            refreshIntegrationsNow()
        }
    }

    private suspend fun refreshIntegrationsNow() {
        integrations.value = try {
            IntegrationsState(client.integrations(settings.serverUrl.value))
        } catch (e: Exception) {
            IntegrationsState(error = e.message ?: e.javaClass.simpleName)
        }
    }

    fun setQuery(q: String) {
        query.value = q
    }

    fun setAllowed(packageName: String, allowed: Boolean) = settings.setAppAllowed(packageName, allowed)

    fun setAll(allowed: Boolean) {
        settings.setBlocked(if (allowed) emptySet() else installed.value.orEmpty().map { it.packageName }.toSet())
    }

    fun setServerUrl(url: String) {
        settings.setServerUrl(url)
        serverStatus.value = ServerStatus.Unknown
    }

    fun setSpeakReplies(enabled: Boolean) = settings.setSpeakReplies(enabled)

    fun setAzureVoice(enabled: Boolean) = settings.setAzureVoice(enabled)

    fun refreshCapabilities() {
        capabilities.update { it.copy(loading = true, error = null) }
        viewModelScope.launch {
            capabilities.value = try {
                val snap = client.permissions(settings.serverUrl.value)
                CapabilitiesState(
                    loading = false,
                    rows = snap.capabilities.map { (name, scopes) ->
                        CapabilityRow(name, scopes, snap.grants.filter { it.capability == name })
                    },
                )
            } catch (e: Exception) {
                CapabilitiesState(loading = false, error = e.message ?: e.javaClass.simpleName)
            }
        }
    }

    fun grantCapability(name: String, scopes: List<String>, ttlSeconds: Long?) = capabilityCall {
        client.grant(settings.serverUrl.value, name, scopes, ttlSeconds)
    }

    fun revokeGrant(grantId: String) = capabilityCall { client.revoke(settings.serverUrl.value, grantId) }

    private fun capabilityCall(call: suspend () -> Unit) {
        viewModelScope.launch {
            try {
                call()
                refreshCapabilities()
            } catch (e: Exception) {
                capabilities.update { it.copy(error = e.message ?: e.javaClass.simpleName) }
            }
        }
    }

    fun checkServer() {
        serverStatus.value = ServerStatus.Checking
        viewModelScope.launch {
            serverStatus.value = try {
                ServerStatus.Online(client.health(settings.serverUrl.value))
            } catch (e: Exception) {
                ServerStatus.Offline(e.message ?: e.javaClass.simpleName)
            }
        }
    }
}
