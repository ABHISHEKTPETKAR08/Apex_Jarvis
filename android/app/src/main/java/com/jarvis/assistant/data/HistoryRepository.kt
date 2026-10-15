package com.jarvis.assistant.data

import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** Conversation history persisted as JSON in app storage. Newest first. */
class HistoryRepository(context: Context, private val scope: CoroutineScope) {
    private val file = File(context.filesDir, "history.json")
    private val writeLock = Mutex()

    private val _items = MutableStateFlow<List<Exchange>>(emptyList())
    val items: StateFlow<List<Exchange>> = _items.asStateFlow()

    init {
        scope.launch(Dispatchers.IO) {
            val loaded = load()
            _items.update { current -> (current + loaded).distinctBy { it.id }.take(MAX_ITEMS) }
        }
    }

    fun add(exchange: Exchange) {
        _items.update { current ->
            // Ids are LazyColumn keys, so they must stay unique.
            val safe = if (current.any { it.id == exchange.id }) exchange.copy(id = current.maxOf { it.id } + 1) else exchange
            listOf(safe) + current.take(MAX_ITEMS - 1)
        }
        persist()
    }

    fun delete(id: Long) {
        _items.update { list -> list.filterNot { it.id == id } }
        persist()
    }

    fun clear() {
        _items.value = emptyList()
        persist()
    }

    private fun persist() {
        val snapshot = _items.value
        scope.launch(Dispatchers.IO) {
            writeLock.withLock {
                val arr = JSONArray()
                snapshot.forEach { e ->
                    arr.put(
                        JSONObject()
                            .put("id", e.id)
                            .put("ts", e.timestamp)
                            .put("user", e.userText)
                            .put("reply", e.reply)
                            .put("actionType", e.action.type)
                            .put("actionValue", e.action.value),
                    )
                }
                file.writeText(arr.toString())
            }
        }
    }

    private fun load(): List<Exchange> = runCatching {
        if (!file.exists()) return emptyList()
        val arr = JSONArray(file.readText())
        List(arr.length()) { i ->
            val o = arr.getJSONObject(i)
            Exchange(
                id = o.getLong("id"),
                timestamp = o.getLong("ts"),
                userText = o.optString("user"),
                reply = o.optString("reply"),
                action = JarvisAction(o.optString("actionType", "none"), o.optString("actionValue")),
            )
        }
    }.getOrDefault(emptyList())

    companion object {
        private const val MAX_ITEMS = 500
    }
}
