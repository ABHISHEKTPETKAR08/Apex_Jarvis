package com.jarvis.assistant.ui.history

import android.app.Application
import android.text.format.DateUtils
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.jarvis.assistant.container
import com.jarvis.assistant.data.Exchange
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import java.text.DateFormat
import java.util.Date

data class HistoryGroup(val title: String, val items: List<Exchange>)

data class HistoryUiState(
    val groups: List<HistoryGroup> = emptyList(),
    val query: String = "",
    val total: Int = 0,
)

class HistoryViewModel(app: Application) : AndroidViewModel(app) {
    private val history = app.container.history
    private val query = MutableStateFlow("")

    val uiState: StateFlow<HistoryUiState> = combine(history.items, query) { items, q ->
        val filtered = if (q.isBlank()) items else items.filter {
            it.userText.contains(q, ignoreCase = true) || it.reply.contains(q, ignoreCase = true)
        }
        HistoryUiState(
            groups = filtered.groupBy { dayLabel(it.timestamp) }.map { (title, list) -> HistoryGroup(title, list) },
            query = q,
            total = items.size,
        )
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), HistoryUiState())

    fun setQuery(q: String) {
        query.value = q
    }

    fun delete(id: Long) = history.delete(id)

    fun clear() = history.clear()

    private fun dayLabel(ts: Long): String = when {
        DateUtils.isToday(ts) -> "Today"
        DateUtils.isToday(ts + DateUtils.DAY_IN_MILLIS) -> "Yesterday"
        else -> DateFormat.getDateInstance(DateFormat.MEDIUM).format(Date(ts))
    }
}
