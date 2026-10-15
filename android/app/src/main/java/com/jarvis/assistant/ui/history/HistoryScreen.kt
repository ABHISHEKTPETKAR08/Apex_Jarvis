package com.jarvis.assistant.ui.history

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.jarvis.assistant.data.Exchange
import com.jarvis.assistant.ui.components.ActionChip
import com.jarvis.assistant.ui.components.EmptyState
import com.jarvis.assistant.ui.components.ScreenHeader
import com.jarvis.assistant.ui.components.SectionHeader
import com.jarvis.assistant.ui.theme.Cyan
import com.jarvis.assistant.ui.theme.Danger
import com.jarvis.assistant.ui.theme.TextMuted
import java.text.DateFormat
import java.util.Date

@Composable
fun HistoryScreen(vm: HistoryViewModel = viewModel()) {
    val state by vm.uiState.collectAsStateWithLifecycle()
    var confirmClear by remember { mutableStateOf(false) }

    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(horizontal = 16.dp, vertical = 8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        item {
            ScreenHeader("History", "${state.total} conversations") {
                if (state.total > 0) {
                    IconButton(onClick = { confirmClear = true }) { Icon(Icons.Filled.Delete, "Clear history", tint = TextMuted) }
                }
            }
        }
        item {
            OutlinedTextField(
                value = state.query,
                onValueChange = vm::setQuery,
                leadingIcon = { Icon(Icons.Filled.Search, null) },
                trailingIcon = {
                    if (state.query.isNotEmpty()) {
                        IconButton(onClick = { vm.setQuery("") }) { Icon(Icons.Filled.Close, "Clear search") }
                    }
                },
                placeholder = { Text("Search conversations") },
                singleLine = true,
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            )
        }
        if (state.groups.isEmpty()) {
            item {
                EmptyState(
                    Icons.AutoMirrored.Filled.List,
                    if (state.query.isBlank()) "No conversations yet" else "No matches",
                    if (state.query.isBlank()) "Talk to JARVIS and your exchanges will show up here." else "Try a different search.",
                )
            }
        }
        state.groups.forEach { group ->
            item(key = "header-${group.title}") { SectionHeader(group.title) }
            items(group.items, key = { it.id }) { ex -> ExchangeCard(ex, onDelete = { vm.delete(ex.id) }) }
        }
        item { Spacer(Modifier.height(16.dp)) }
    }

    if (confirmClear) {
        AlertDialog(
            onDismissRequest = { confirmClear = false },
            title = { Text("Clear history?") },
            text = { Text("This permanently deletes all ${state.total} conversations.") },
            confirmButton = {
                TextButton(onClick = { vm.clear(); confirmClear = false }) { Text("Clear", color = Danger) }
            },
            dismissButton = { TextButton(onClick = { confirmClear = false }) { Text("Cancel") } },
            containerColor = MaterialTheme.colorScheme.surfaceContainerHigh,
        )
    }
}

@Composable
private fun ExchangeCard(ex: Exchange, onDelete: () -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    Card(
        onClick = { expanded = !expanded },
        shape = RoundedCornerShape(16.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer),
    ) {
        Column(Modifier.fillMaxWidth().padding(start = 14.dp, top = 12.dp, bottom = 12.dp, end = 4.dp)) {
            Row(verticalAlignment = Alignment.Top) {
                Column(Modifier.weight(1f)) {
                    Text(
                        DateFormat.getTimeInstance(DateFormat.SHORT).format(Date(ex.timestamp)),
                        style = MaterialTheme.typography.labelSmall,
                        color = TextMuted,
                    )
                    Spacer(Modifier.height(4.dp))
                    Text(ex.userText, style = MaterialTheme.typography.titleMedium, maxLines = if (expanded) Int.MAX_VALUE else 1, overflow = TextOverflow.Ellipsis)
                    Spacer(Modifier.height(4.dp))
                    Text(
                        ex.reply,
                        style = MaterialTheme.typography.bodyMedium,
                        color = Cyan.copy(alpha = 0.9f),
                        maxLines = if (expanded) Int.MAX_VALUE else 2,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
                IconButton(onClick = onDelete) { Icon(Icons.Filled.Close, "Delete", tint = TextMuted) }
            }
            if (!ex.action.isNone) {
                Spacer(Modifier.height(8.dp))
                ActionChip(ex.action.type, ex.action.value)
            }
        }
    }
}
