package com.jarvis.assistant.ui.apps

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.jarvis.assistant.data.IntegrationStatus
import com.jarvis.assistant.ui.components.PanelCard
import com.jarvis.assistant.ui.components.StatusPill
import com.jarvis.assistant.ui.theme.Amber
import com.jarvis.assistant.ui.theme.Cyan
import com.jarvis.assistant.ui.theme.Danger
import com.jarvis.assistant.ui.theme.Navy
import com.jarvis.assistant.ui.theme.Success
import com.jarvis.assistant.ui.theme.TextMuted

private val LABELS = mapOf(
    "microsoft_graph" to "Microsoft 365 (Outlook mail + calendar)",
    "whatsapp_business" to "WhatsApp Business Platform",
    "azure_speech" to "Azure Speech (neural voice)",
)

private fun stateColor(state: String) = when (state) {
    "CONNECTED", "CONFIGURED" -> Success
    "CONNECTING" -> Amber
    "NOT_CONFIGURED" -> TextMuted
    else -> Amber
}

@Composable
fun IntegrationsCard(
    state: IntegrationsState,
    onConnectGraph: () -> Unit,
    onDisconnectGraph: () -> Unit,
    onRefresh: () -> Unit,
    azureVoice: Boolean,
    onAzureVoice: (Boolean) -> Unit,
) {
    val context = LocalContext.current
    PanelCard {
        Text(
            "Keys and sign-in tokens stay on the JARVIS server. NOT_CONFIGURED means the server owner hasn't set it up in server/.env.",
            style = MaterialTheme.typography.bodySmall,
            color = TextMuted,
        )
        state.error?.let { Text(it, color = Danger, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 6.dp)) }
        state.items.forEachIndexed { i, item ->
            HorizontalDivider(Modifier.padding(vertical = 10.dp), color = MaterialTheme.colorScheme.outlineVariant)
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(LABELS[item.name] ?: item.name, style = MaterialTheme.typography.bodyLarge)
                    val sub = item.extra["account"] ?: item.detail
                    if (!sub.isNullOrBlank()) Text(sub, style = MaterialTheme.typography.bodySmall, color = TextMuted)
                }
                StatusPill(item.state.replace('_', ' '), stateColor(item.state))
            }
            if (item.name == "microsoft_graph") {
                GraphActions(item, onConnectGraph, onDisconnectGraph, context)
            }
            if (item.name == "azure_speech") {
                Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 6.dp)) {
                    Text(
                        "Speak replies with the Azure voice (needs the speaker grant)",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextMuted,
                        modifier = Modifier.weight(1f),
                    )
                    androidx.compose.material3.Switch(
                        checked = azureVoice && item.state == "CONFIGURED",
                        enabled = item.state == "CONFIGURED",
                        onCheckedChange = onAzureVoice,
                    )
                }
            }
        }
        if (state.items.isEmpty() && state.error == null) {
            Text("Loading…", style = MaterialTheme.typography.bodySmall, color = TextMuted, modifier = Modifier.padding(top = 8.dp))
        }
        TextButton(onClick = onRefresh) { Text("Refresh") }
    }
}

@Composable
private fun GraphActions(item: IntegrationStatus, onConnect: () -> Unit, onDisconnect: () -> Unit, context: Context) {
    when (item.state) {
        "NOT_CONNECTED" -> TextButton(onClick = onConnect) { Text("Connect Microsoft account") }
        "CONNECTED" -> TextButton(onClick = onDisconnect) { Text("Disconnect", color = Danger) }
        "CONNECTING" -> {
            val code = item.extra["user_code"].orEmpty()
            val uri = item.extra["verification_uri"].orEmpty()
            Spacer(Modifier.height(8.dp))
            Text("Sign in at $uri with code:", style = MaterialTheme.typography.bodySmall, color = TextMuted)
            Text(code, fontFamily = FontFamily.Monospace, fontWeight = FontWeight.Bold, fontSize = 22.sp, color = Cyan)
            Button(
                onClick = {
                    val cm = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                    cm.setPrimaryClip(ClipData.newPlainText("Microsoft sign-in code", code))
                    if (uri.startsWith("https://")) context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(uri)))
                },
                colors = ButtonDefaults.buttonColors(containerColor = Cyan, contentColor = Navy),
                modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
            ) { Text("Copy code and open sign-in") }
        }
    }
}
