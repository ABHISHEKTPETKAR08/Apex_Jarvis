package com.jarvis.assistant.ui.apps

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Call
import androidx.compose.material.icons.filled.Face
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleEventEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.jarvis.assistant.ui.components.EmptyState
import com.jarvis.assistant.ui.components.MicIcon
import com.jarvis.assistant.ui.components.PanelCard
import com.jarvis.assistant.ui.components.ScreenHeader
import com.jarvis.assistant.ui.components.SectionHeader
import com.jarvis.assistant.ui.components.StatusPill
import com.jarvis.assistant.ui.theme.Amber
import com.jarvis.assistant.ui.theme.Cyan
import com.jarvis.assistant.ui.theme.Danger
import com.jarvis.assistant.ui.theme.Navy
import com.jarvis.assistant.ui.theme.Success
import com.jarvis.assistant.ui.theme.TextMuted

@Composable
fun AppAccessScreen(vm: AppAccessViewModel = viewModel()) {
    val state by vm.uiState.collectAsStateWithLifecycle()

    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(horizontal = 16.dp, vertical = 8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        item { ScreenHeader("App Access", "Control what JARVIS can reach") }

        item { SectionHeader("Connection") }
        item {
            ConnectionCard(
                url = state.serverUrl,
                status = state.serverStatus,
                speakReplies = state.speakReplies,
                onUrlChange = vm::setServerUrl,
                onCheck = {
                    vm.checkServer()
                    vm.refreshCapabilities()
                    vm.refreshIntegrations()
                },
                onSpeakChange = vm::setSpeakReplies,
            )
        }

        item { SectionHeader("Integrations") }
        item {
            IntegrationsCard(
                state = state.integrations,
                onConnectGraph = vm::connectGraph,
                onDisconnectGraph = vm::disconnectGraph,
                onRefresh = vm::refreshIntegrations,
                azureVoice = state.azureVoice,
                onAzureVoice = vm::setAzureVoice,
            )
        }

        item { SectionHeader("JARVIS capabilities") }
        item {
            CapabilitiesCard(
                state = state.capabilities,
                onGrant = vm::grantCapability,
                onRevoke = vm::revokeGrant,
                onRetry = vm::refreshCapabilities,
            )
        }

        item { SectionHeader("Device permissions") }
        item { PermissionsCard() }

        item {
            SectionHeader("Apps · ${state.allowedCount}/${state.totalCount} allowed") {
                Row {
                    TextButton(onClick = { vm.setAll(true) }) { Text("Allow all") }
                    TextButton(onClick = { vm.setAll(false) }) { Text("Block all", color = Danger) }
                }
            }
        }
        item {
            OutlinedTextField(
                value = state.query,
                onValueChange = vm::setQuery,
                leadingIcon = { Icon(Icons.Filled.Search, null) },
                placeholder = { Text("Search apps") },
                singleLine = true,
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier.fillMaxWidth(),
            )
        }

        when {
            state.loading -> item {
                Box(Modifier.fillMaxWidth().padding(32.dp), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
            }
            state.apps.isEmpty() -> item {
                EmptyState(Icons.Filled.Search, "No apps found", "Try a different search.")
            }
            else -> items(state.apps, key = { it.app.packageName }) { row ->
                AppRowItem(row, onToggle = { vm.setAllowed(row.app.packageName, it) })
            }
        }
        item { Spacer(Modifier.height(16.dp)) }
    }
}

@Composable
private fun ConnectionCard(
    url: String,
    status: ServerStatus,
    speakReplies: Boolean,
    onUrlChange: (String) -> Unit,
    onCheck: () -> Unit,
    onSpeakChange: (Boolean) -> Unit,
) {
    var draft by remember(url) { mutableStateOf(url) }
    PanelCard {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("JARVIS server", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
            when (status) {
                ServerStatus.Unknown -> StatusPill("UNKNOWN", TextMuted)
                ServerStatus.Checking -> StatusPill("CHECKING", Amber)
                is ServerStatus.Online -> StatusPill("ONLINE · ${status.mode.uppercase()}", Success)
                is ServerStatus.Offline -> StatusPill("OFFLINE", Danger)
            }
        }
        if (status is ServerStatus.Offline) {
            Text(status.reason, style = MaterialTheme.typography.bodySmall, color = Danger, modifier = Modifier.padding(top = 4.dp))
        }
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(
            value = draft,
            onValueChange = { draft = it },
            label = { Text("Server URL") },
            singleLine = true,
            shape = RoundedCornerShape(12.dp),
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(
                onClick = { onUrlChange(draft); onCheck() },
                colors = ButtonDefaults.buttonColors(containerColor = Cyan, contentColor = Navy),
            ) { Text("Save & test") }
            OutlinedButton(onClick = { draft = "http://127.0.0.1:8000" }) { Text("USB default") }
        }
        Text(
            "Over USB run: adb reverse tcp:8000 tcp:8000",
            style = MaterialTheme.typography.bodySmall,
            color = TextMuted,
            modifier = Modifier.padding(top = 8.dp),
        )
        HorizontalDivider(Modifier.padding(vertical = 12.dp), color = MaterialTheme.colorScheme.outlineVariant)
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("Speak replies", style = MaterialTheme.typography.bodyLarge)
                Text("Read responses aloud with text-to-speech", style = MaterialTheme.typography.bodySmall, color = TextMuted)
            }
            Switch(checked = speakReplies, onCheckedChange = onSpeakChange, colors = jarvisSwitchColors())
        }
    }
}

@Composable
private fun PermissionsCard() {
    val context = LocalContext.current
    var refresh by remember { mutableIntStateOf(0) }
    var lastRequested by remember { mutableStateOf<String?>(null) }
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        val activity = context as? android.app.Activity
        val permission = lastRequested
        // Denied with "don't ask again": the only way back is the system settings page.
        if (!granted && permission != null && activity != null && !activity.shouldShowRequestPermissionRationale(permission)) {
            context.startActivity(
                android.content.Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
                    .setData(android.net.Uri.fromParts("package", context.packageName, null)),
            )
        }
        refresh++
    }
    fun granted(p: String) = context.checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED
    fun request(p: String) {
        lastRequested = p
        launcher.launch(p)
    }
    LifecycleEventEffect(androidx.lifecycle.Lifecycle.Event.ON_RESUME) { refresh++ } // back from Settings

    val micGranted = remember(refresh) { granted(Manifest.permission.RECORD_AUDIO) }
    val cameraGranted = remember(refresh) { granted(Manifest.permission.CAMERA) }
    val contactsGranted = remember(refresh) { granted(Manifest.permission.READ_CONTACTS) }

    PanelCard {
        PermissionRow(MicIcon, "Microphone", "Only while you tap the core", micGranted) {
            request(Manifest.permission.RECORD_AUDIO)
        }
        HorizontalDivider(Modifier.padding(vertical = 10.dp), color = MaterialTheme.colorScheme.outlineVariant)
        PermissionRow(Icons.Filled.Face, "Camera", "Photos you ask for", cameraGranted) {
            request(Manifest.permission.CAMERA)
        }
        HorizontalDivider(Modifier.padding(vertical = 10.dp), color = MaterialTheme.colorScheme.outlineVariant)
        PermissionRow(Icons.Filled.Call, "Contacts", "Call people by name", contactsGranted) {
            request(Manifest.permission.READ_CONTACTS)
        }
    }
}

@Composable
private fun PermissionRow(icon: ImageVector, title: String, subtitle: String, granted: Boolean, onRequest: () -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        IconBadge(icon, if (granted) Success else Amber)
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            Text(subtitle, style = MaterialTheme.typography.bodySmall, color = TextMuted)
        }
        if (granted) StatusPill("GRANTED", Success) else TextButton(onClick = onRequest) { Text("Grant") }
    }
}

@Composable
private fun AppRowItem(row: AppRow, onToggle: (Boolean) -> Unit) {
    Card(
        shape = RoundedCornerShape(16.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer),
    ) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
            val icon = row.app.icon
            if (icon != null) {
                Image(icon, null, Modifier.size(40.dp).clip(RoundedCornerShape(10.dp)))
            } else {
                IconBadge(Icons.Filled.Lock, TextMuted)
            }
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(row.app.label, style = MaterialTheme.typography.bodyLarge, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(
                    if (row.allowed) "JARVIS can open this app" else "Blocked",
                    style = MaterialTheme.typography.bodySmall,
                    color = if (row.allowed) TextMuted else Danger,
                )
            }
            Switch(checked = row.allowed, onCheckedChange = onToggle, colors = jarvisSwitchColors())
        }
    }
}

@Composable
private fun IconBadge(icon: ImageVector, color: androidx.compose.ui.graphics.Color) {
    Box(
        Modifier.size(40.dp).clip(RoundedCornerShape(10.dp)).background(color.copy(alpha = 0.14f)),
        contentAlignment = Alignment.Center,
    ) { Icon(icon, null, tint = color, modifier = Modifier.size(20.dp)) }
}

@Composable
private fun jarvisSwitchColors() = SwitchDefaults.colors(
    checkedThumbColor = Navy,
    checkedTrackColor = Cyan,
    uncheckedThumbColor = TextMuted,
    uncheckedTrackColor = MaterialTheme.colorScheme.surfaceContainerHigh,
    uncheckedBorderColor = MaterialTheme.colorScheme.outline,
)
