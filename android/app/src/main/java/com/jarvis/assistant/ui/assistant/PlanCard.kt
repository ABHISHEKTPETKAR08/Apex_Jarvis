package com.jarvis.assistant.ui.assistant

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.jarvis.assistant.data.PlanConfirmation
import com.jarvis.assistant.data.PlanState
import com.jarvis.assistant.data.PlanStep
import com.jarvis.assistant.ui.components.StatusPill
import com.jarvis.assistant.ui.theme.Amber
import com.jarvis.assistant.ui.theme.Cyan
import com.jarvis.assistant.ui.theme.Danger
import com.jarvis.assistant.ui.theme.Navy
import com.jarvis.assistant.ui.theme.Success
import com.jarvis.assistant.ui.theme.TextMuted
import com.jarvis.assistant.ui.theme.Violet

private fun planStatusLabel(status: String) = when (status) {
    "awaiting_approval" -> "NEEDS APPROVAL"
    "awaiting_confirmation" -> "CONFIRM"
    "awaiting_result", "running" -> "RUNNING"
    "blocked" -> "NEEDS PERMISSION"
    "awaiting_verification" -> "VERIFY"
    "retry_scheduled" -> "RETRYING"
    "step_failed" -> "STEP FAILED"
    else -> status.uppercase()
}

private fun planStatusColor(status: String): Color = when (status) {
    "completed" -> Success
    "cancelled" -> TextMuted
    "blocked", "awaiting_approval", "awaiting_confirmation", "awaiting_verification" -> Amber
    "step_failed" -> Danger
    else -> Violet
}

private fun stepColor(status: String): Color = when (status) {
    "succeeded" -> Success
    "failed" -> Danger
    "blocked", "awaiting_confirmation", "handed_off", "uncertain", "retry_scheduled" -> Amber
    "dispatched" -> Violet
    "cancelled" -> TextMuted
    else -> Color(0xFF3A4A63)
}

@Composable
fun PlanCard(
    plan: PlanState,
    busy: Boolean,
    onApprove: () -> Unit,
    onCancel: () -> Unit,
    onResume: () -> Unit,
    onRetry: (Int) -> Unit,
    onDismiss: () -> Unit,
) {
    Card(
        shape = RoundedCornerShape(20.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer),
        border = BorderStroke(1.dp, planStatusColor(plan.status).copy(alpha = 0.4f)),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("PLAN", style = MaterialTheme.typography.labelSmall, color = TextMuted, modifier = Modifier.weight(1f))
                StatusPill(planStatusLabel(plan.status), planStatusColor(plan.status))
            }
            Spacer(Modifier.height(6.dp))
            Text(plan.goal, style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(10.dp))
            LinearProgressIndicator(
                progress = { if (plan.total == 0) 0f else plan.done.toFloat() / plan.total },
                modifier = Modifier.fillMaxWidth().height(6.dp).clip(RoundedCornerShape(3.dp)),
                color = Cyan,
                trackColor = MaterialTheme.colorScheme.surfaceContainerHigh,
            )
            Text(
                "${plan.done} of ${plan.total} steps done",
                style = MaterialTheme.typography.labelSmall,
                color = TextMuted,
                modifier = Modifier.padding(top = 4.dp, bottom = 8.dp),
            )
            plan.steps.forEach { step -> StepRow(step, canRetry = !busy && step.status in setOf("failed", "blocked"), onRetry = { onRetry(step.id) }) }
            if (plan.note.isNotBlank()) {
                Text(plan.note, style = MaterialTheme.typography.bodySmall, color = planStatusColor(plan.status), modifier = Modifier.padding(top = 8.dp))
            }
            Spacer(Modifier.height(12.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                val primary = ButtonDefaults.buttonColors(containerColor = Cyan, contentColor = Navy)
                when (plan.status) {
                    "awaiting_approval" -> {
                        Button(onClick = onApprove, enabled = !busy, colors = primary) { Text("Approve plan") }
                        OutlinedButton(onClick = onCancel, enabled = !busy) { Text("Cancel") }
                    }
                    "blocked" -> {
                        Button(onClick = onResume, enabled = !busy, colors = primary) { Text("Resume") }
                        OutlinedButton(onClick = onCancel, enabled = !busy) { Text("Cancel") }
                    }
                    "completed", "cancelled" -> TextButton(onClick = onDismiss) { Text("Dismiss") }
                    else -> OutlinedButton(onClick = onCancel, enabled = !busy) { Text("Cancel") }
                }
            }
        }
    }
}

@Composable
private fun StepRow(step: PlanStep, canRetry: Boolean, onRetry: () -> Unit) {
    Row(Modifier.fillMaxWidth().padding(vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(22.dp).clip(CircleShape).background(stepColor(step.status).copy(alpha = 0.2f)), contentAlignment = Alignment.Center) {
            Text("${step.id + 1}", style = MaterialTheme.typography.labelSmall, color = stepColor(step.status))
        }
        Spacer(Modifier.width(10.dp))
        Column(Modifier.weight(1f)) {
            Text(step.summary, style = MaterialTheme.typography.bodyMedium, maxLines = 3)
            val meta = listOfNotNull(
                step.status.replace('_', ' '),
                "attempt ${step.attempts}".takeIf { step.attempts > 1 },
                step.capability?.let { "needs $it" },
                "confirm first".takeIf { step.consequential },
            ).joinToString(" · ")
            Text(meta, style = MaterialTheme.typography.labelSmall, color = stepColor(step.status))
            if (step.message.isNotBlank()) Text(step.message, style = MaterialTheme.typography.bodySmall, color = TextMuted)
        }
        if (canRetry) TextButton(onClick = onRetry) { Text("Retry") }
    }
}

@Composable
fun VerifyStepDialog(verification: PlanConfirmation, onAnswer: (Boolean) -> Unit) {
    AlertDialog(
        onDismissRequest = {}, // must be an explicit answer; JARVIS won't guess
        title = { Text("Did this happen?") },
        text = {
            Column {
                Text(verification.summary, style = MaterialTheme.typography.bodyLarge, fontWeight = FontWeight.Medium)
                Spacer(Modifier.height(6.dp))
                Text(
                    "JARVIS opened it but can't see whether it was completed. It won't retry until you answer.",
                    color = TextMuted,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        },
        confirmButton = {
            Button(onClick = { onAnswer(true) }, colors = ButtonDefaults.buttonColors(containerColor = Cyan, contentColor = Navy)) {
                Text("Yes, it happened")
            }
        },
        dismissButton = { TextButton(onClick = { onAnswer(false) }) { Text("No", color = Danger) } },
        containerColor = MaterialTheme.colorScheme.surfaceContainerHigh,
    )
}

@Composable
fun ConfirmStepDialog(confirmation: PlanConfirmation, onConfirm: () -> Unit, onDecline: () -> Unit) {
    AlertDialog(
        onDismissRequest = {},  // must be an explicit choice
        title = { Text("Confirm before I proceed") },
        text = {
            Column {
                Text("JARVIS will:", color = TextMuted, style = MaterialTheme.typography.bodySmall)
                Spacer(Modifier.height(6.dp))
                Text(confirmation.summary, style = MaterialTheme.typography.bodyLarge, fontWeight = FontWeight.Medium)
            }
        },
        confirmButton = {
            Button(onClick = onConfirm, colors = ButtonDefaults.buttonColors(containerColor = Cyan, contentColor = Navy)) {
                Text("Confirm")
            }
        },
        dismissButton = { TextButton(onClick = onDecline) { Text("Don't send", color = Danger) } },
        containerColor = MaterialTheme.colorScheme.surfaceContainerHigh,
    )
}
