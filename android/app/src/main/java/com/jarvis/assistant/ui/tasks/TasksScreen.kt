package com.jarvis.assistant.ui.tasks

import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Build
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.jarvis.assistant.data.ActionExecutor
import com.jarvis.assistant.data.PlanState
import com.jarvis.assistant.data.TaskRecord
import com.jarvis.assistant.ui.components.EmptyState
import com.jarvis.assistant.ui.components.PanelCard
import com.jarvis.assistant.ui.components.ScreenHeader
import com.jarvis.assistant.ui.components.SectionHeader
import com.jarvis.assistant.ui.components.StatusPill
import com.jarvis.assistant.ui.components.actionColor
import com.jarvis.assistant.ui.components.actionIcon
import com.jarvis.assistant.ui.components.statusColor
import com.jarvis.assistant.ui.theme.Amber
import com.jarvis.assistant.ui.theme.Cyan
import com.jarvis.assistant.ui.theme.Danger
import com.jarvis.assistant.ui.theme.Navy
import com.jarvis.assistant.ui.theme.Success
import com.jarvis.assistant.ui.theme.TextMuted
import java.text.DateFormat
import java.util.Date

@Composable
fun TasksScreen(vm: TasksViewModel = viewModel()) {
    val state by vm.uiState.collectAsStateWithLifecycle()

    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(horizontal = 16.dp, vertical = 8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        item {
            ScreenHeader("Task Execution", "Run device actions and review results") {
                if (state.tasks.isNotEmpty()) {
                    IconButton(onClick = vm::clear) { Icon(Icons.Filled.Delete, "Clear log", tint = TextMuted) }
                }
            }
        }
        item {
            Row(Modifier.fillMaxWidth().padding(top = 8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                StatTile("Succeeded", state.succeeded, Success, Modifier.weight(1f))
                StatTile("Failed", state.failed, Danger, Modifier.weight(1f))
                StatTile("Blocked", state.blocked, Amber, Modifier.weight(1f))
            }
        }
        item { SectionHeader("Quick execute") }
        item {
            QuickExecuteCard(
                selectedType = state.selectedType,
                value = state.value,
                lastResult = state.lastResult,
                onSelect = vm::selectType,
                onValueChange = vm::setValue,
                onRun = vm::runDraft,
            )
        }
        item {
            SectionHeader("Plan history") {
                IconButton(onClick = vm::refreshHistory) { Icon(Icons.Filled.Refresh, "Refresh plan history", tint = Cyan) }
            }
        }
        state.history.error?.let { err ->
            item { Text("Couldn't load plan history: $err", color = Danger, style = MaterialTheme.typography.bodySmall) }
        }
        if (state.history.plans.isEmpty() && state.history.error == null && !state.history.loading) {
            item { Text("No plans yet.", color = TextMuted, style = MaterialTheme.typography.bodySmall) }
        }
        items(state.history.plans, key = { "plan-${it.id}" }) { plan -> PlanHistoryCard(plan) }

        item { SectionHeader("Direct actions") }
        if (state.tasks.isEmpty()) {
            item { EmptyState(Icons.Filled.Build, "No tasks yet", "Actions from voice commands appear here.") }
        } else {
            items(state.tasks, key = { it.id }) { task -> TaskCard(task, onRetry = { vm.retry(task) }) }
        }
        item { Spacer(Modifier.height(16.dp)) }
    }
}

@Composable
private fun StatTile(label: String, count: Int, color: Color, modifier: Modifier = Modifier) {
    Card(
        modifier = modifier,
        shape = RoundedCornerShape(16.dp),
        colors = CardDefaults.cardColors(containerColor = color.copy(alpha = 0.08f)),
        border = androidx.compose.foundation.BorderStroke(1.dp, color.copy(alpha = 0.25f)),
    ) {
        Column(Modifier.padding(14.dp)) {
            Text(count.toString(), style = MaterialTheme.typography.headlineSmall, color = color)
            Text(label.uppercase(), style = MaterialTheme.typography.labelSmall, color = TextMuted)
        }
    }
}

@Composable
private fun QuickExecuteCard(
    selectedType: String,
    value: String,
    lastResult: TaskRecord?,
    onSelect: (String) -> Unit,
    onValueChange: (String) -> Unit,
    onRun: () -> Unit,
) {
    PanelCard {
        Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            ActionExecutor.SUPPORTED.forEach { type ->
                FilterChip(
                    selected = type == selectedType,
                    onClick = { onSelect(type) },
                    label = { Text(ActionExecutor.label(type)) },
                    leadingIcon = { Icon(actionIcon(type), null, Modifier.size(16.dp)) },
                    colors = FilterChipDefaults.filterChipColors(
                        selectedContainerColor = actionColor(type).copy(alpha = 0.18f),
                        selectedLabelColor = actionColor(type),
                        selectedLeadingIconColor = actionColor(type),
                    ),
                )
            }
        }
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(
            value = value,
            onValueChange = onValueChange,
            placeholder = { Text(ActionExecutor.hint(selectedType)) },
            singleLine = true,
            shape = RoundedCornerShape(12.dp),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Go),
            keyboardActions = KeyboardActions(onGo = { onRun() }),
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(12.dp))
        Button(
            onClick = onRun,
            enabled = value.isNotBlank(),
            colors = ButtonDefaults.buttonColors(containerColor = Cyan, contentColor = Navy),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Icon(Icons.Filled.PlayArrow, null)
            Spacer(Modifier.width(6.dp))
            Text("Execute")
        }
        lastResult?.let {
            Text(
                it.message,
                style = MaterialTheme.typography.bodySmall,
                color = statusColor(it.status),
                modifier = Modifier.padding(top = 8.dp),
            )
        }
    }
}

private fun planColor(status: String): Color = when (status) {
    "completed" -> Success
    "cancelled" -> TextMuted
    "step_failed" -> Danger
    "awaiting_approval", "awaiting_confirmation", "awaiting_verification", "blocked", "retry_scheduled" -> Amber
    else -> Cyan
}

private fun stepTint(status: String): Color = when (status) {
    "succeeded" -> Success
    "failed" -> Danger
    "cancelled", "pending" -> TextMuted
    else -> Amber
}

@Composable
private fun PlanHistoryCard(plan: PlanState) {
    val fmt = DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.SHORT)
    Card(
        shape = RoundedCornerShape(16.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer),
    ) {
        Column(Modifier.fillMaxWidth().padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(plan.goal, style = MaterialTheme.typography.bodyLarge, maxLines = 2, modifier = Modifier.weight(1f))
                Spacer(Modifier.width(8.dp))
                StatusPill(plan.status.replace('_', ' ').uppercase(), planColor(plan.status))
            }
            Text(
                buildString {
                    append("${plan.done}/${plan.total} done · started ${fmt.format(Date((plan.createdAt * 1000).toLong()))}")
                    plan.finishedAt?.let { append(" · ended ${fmt.format(Date((it * 1000).toLong()))}") }
                },
                style = MaterialTheme.typography.labelSmall,
                color = TextMuted,
                modifier = Modifier.padding(top = 4.dp, bottom = 6.dp),
            )
            plan.steps.forEach { step ->
                Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(vertical = 2.dp)) {
                    Box(Modifier.size(8.dp).clip(RoundedCornerShape(4.dp)).background(stepTint(step.status)))
                    Spacer(Modifier.width(8.dp))
                    Text(step.summary, style = MaterialTheme.typography.bodySmall, maxLines = 1, modifier = Modifier.weight(1f))
                    Text(
                        step.status.replace('_', ' ') + if (step.attempts > 1) " ×${step.attempts}" else "",
                        style = MaterialTheme.typography.labelSmall,
                        color = stepTint(step.status),
                    )
                }
            }
        }
    }
}

@Composable
private fun TaskCard(task: TaskRecord, onRetry: () -> Unit) {
    val color = actionColor(task.action.type)
    Card(
        shape = RoundedCornerShape(16.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer),
    ) {
        Row(Modifier.fillMaxWidth().padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(
                Modifier.size(40.dp).clip(RoundedCornerShape(12.dp)).background(color.copy(alpha = 0.14f)),
                contentAlignment = Alignment.Center,
            ) { Icon(actionIcon(task.action.type), null, tint = color, modifier = Modifier.size(20.dp)) }
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(
                    "${ActionExecutor.label(task.action.type)} · ${task.action.value}",
                    style = MaterialTheme.typography.bodyLarge,
                    maxLines = 1,
                )
                Text(task.message, style = MaterialTheme.typography.bodySmall, color = TextMuted, maxLines = 2)
                Spacer(Modifier.height(6.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    StatusPill(task.status.name, statusColor(task.status))
                    Spacer(Modifier.width(8.dp))
                    Text(
                        "${task.source} · ${DateFormat.getTimeInstance(DateFormat.SHORT).format(Date(task.timestamp))}",
                        style = MaterialTheme.typography.labelSmall,
                        color = TextMuted,
                    )
                }
            }
            IconButton(onClick = onRetry) { Icon(Icons.Filled.Refresh, "Run again", tint = Cyan) }
        }
    }
}
