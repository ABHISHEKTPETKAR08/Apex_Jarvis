package com.jarvis.assistant.ui.apps

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.DateRange
import androidx.compose.material.icons.filled.Email
import androidx.compose.material.icons.filled.Face
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import com.jarvis.assistant.data.CapabilityGrant
import com.jarvis.assistant.ui.components.MicIcon
import com.jarvis.assistant.ui.components.PanelCard
import com.jarvis.assistant.ui.components.StatusPill
import com.jarvis.assistant.ui.theme.Amber
import com.jarvis.assistant.ui.theme.Cyan
import com.jarvis.assistant.ui.theme.Danger
import com.jarvis.assistant.ui.theme.Navy
import com.jarvis.assistant.ui.theme.Success
import com.jarvis.assistant.ui.theme.TextMuted

private fun capabilityIcon(name: String): ImageVector = when (name) {
    "email" -> Icons.Filled.Email
    "whatsapp" -> Icons.AutoMirrored.Filled.Send
    "camera" -> Icons.Filled.Face
    "microphone" -> MicIcon
    "speaker" -> Icons.Filled.Notifications
    else -> Icons.Filled.DateRange
}

private val DURATIONS = listOf<Pair<String, Long?>>(
    "1 hour" to 3_600L,
    "24 hours" to 86_400L,
    "7 days" to 604_800L,
    "Until revoked" to null,
)

private fun expiryText(g: CapabilityGrant): String {
    val exp = g.expiresAt ?: return "until revoked"
    val mins = ((exp * 1000 - System.currentTimeMillis()) / 60_000).toLong()
    return when {
        mins < 1 -> "expiring"
        mins < 120 -> "expires in $mins min"
        mins < 48 * 60 -> "expires in ${mins / 60} h"
        else -> "expires in ${mins / 1440} days"
    }
}

@Composable
fun CapabilitiesCard(
    state: CapabilitiesState,
    onGrant: (String, List<String>, Long?) -> Unit,
    onRevoke: (String) -> Unit,
    onRetry: () -> Unit,
) {
    var granting by remember { mutableStateOf<CapabilityRow?>(null) }
    PanelCard {
        Text(
            "Denied unless granted. Sending email/WhatsApp and adding events also ask for confirmation every time.",
            style = MaterialTheme.typography.bodySmall,
            color = TextMuted,
        )
        when {
            state.loading && state.rows.isEmpty() ->
                Box(Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
            state.rows.isEmpty() -> {
                Text(state.error ?: "Server unavailable.", color = Danger, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 8.dp))
                TextButton(onClick = onRetry) { Text("Retry") }
            }
            else -> {
                state.error?.let { Text(it, color = Danger, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 8.dp)) }
                state.rows.forEachIndexed { i, row ->
                    if (i > 0) HorizontalDivider(Modifier.padding(vertical = 8.dp), color = MaterialTheme.colorScheme.outlineVariant)
                    else Spacer(Modifier.padding(top = 8.dp))
                    CapabilityRowItem(row, onGrant = { granting = row }, onRevoke = onRevoke)
                }
            }
        }
    }
    granting?.let { row ->
        GrantDialog(
            row = row,
            onDismiss = { granting = null },
            onGrant = { scopes, ttl ->
                onGrant(row.name, scopes, ttl)
                granting = null
            },
        )
    }
}

@Composable
private fun CapabilityRowItem(row: CapabilityRow, onGrant: () -> Unit, onRevoke: (String) -> Unit) {
    val active = row.grants.isNotEmpty()
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(
            Modifier.size(40.dp).clip(RoundedCornerShape(10.dp)).background((if (active) Success else TextMuted).copy(alpha = 0.14f)),
            contentAlignment = Alignment.Center,
        ) { Icon(capabilityIcon(row.name), null, tint = if (active) Success else TextMuted, modifier = Modifier.size(20.dp)) }
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(row.name.replaceFirstChar { it.uppercase() }, style = MaterialTheme.typography.bodyLarge)
            if (!active) Text("Denied", style = MaterialTheme.typography.bodySmall, color = TextMuted)
        }
        if (active) StatusPill("GRANTED", Success) else TextButton(onClick = onGrant) { Text("Grant") }
    }
    row.grants.forEach { g ->
        Row(Modifier.fillMaxWidth().padding(start = 52.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(
                g.scopes.joinToString(", ") + " · " + expiryText(g) +
                    (if (g.targets.isNotEmpty()) " · only ${g.targets.joinToString()}" else ""),
                style = MaterialTheme.typography.bodySmall,
                color = Amber,
                modifier = Modifier.weight(1f),
            )
            TextButton(onClick = { onRevoke(g.id) }) { Text("Revoke", color = Danger) }
        }
    }
    if (active) {
        TextButton(onClick = onGrant, modifier = Modifier.padding(start = 44.dp)) { Text("Add grant") }
    }
}

@Composable
private fun GrantDialog(row: CapabilityRow, onDismiss: () -> Unit, onGrant: (List<String>, Long?) -> Unit) {
    var scopes by remember { mutableStateOf(row.scopes.toSet()) }
    var duration by remember { mutableStateOf(DURATIONS.first()) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Grant ${row.name}") },
        text = {
            Column {
                Text("Allow", style = MaterialTheme.typography.labelSmall, color = TextMuted)
                row.scopes.forEach { s ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Checkbox(checked = s in scopes, onCheckedChange = { on -> scopes = if (on) scopes + s else scopes - s })
                        Text(s)
                    }
                }
                Text("For", style = MaterialTheme.typography.labelSmall, color = TextMuted, modifier = Modifier.padding(top = 8.dp))
                DURATIONS.forEach { d ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        RadioButton(selected = d == duration, onClick = { duration = d })
                        Text(d.first)
                    }
                }
            }
        },
        confirmButton = {
            Button(
                onClick = { onGrant(row.scopes.filter { it in scopes }, duration.second) },
                enabled = scopes.isNotEmpty(),
                colors = ButtonDefaults.buttonColors(containerColor = Cyan, contentColor = Navy),
            ) { Text("Grant") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
        containerColor = MaterialTheme.colorScheme.surfaceContainerHigh,
    )
}
