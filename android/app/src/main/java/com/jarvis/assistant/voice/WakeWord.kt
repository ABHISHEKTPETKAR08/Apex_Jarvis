package com.jarvis.assistant.voice

/** Hands-free only acts on speech addressed to JARVIS. Pure Kotlin for unit tests. */
object WakeWord {
    private val WAKE = Regex(
        "^\\s*(?:hey |ok |okay )?(?:jarvis|jarviss|jervis|javis)\\b[\\s,.!?:]*(.*)$",
        RegexOption.IGNORE_CASE,
    )

    /** Text after the wake word, "" for just "Jarvis", null when the wake word wasn't said first. */
    fun command(heard: String): String? = WAKE.find(heard)?.groupValues?.get(1)?.trim()
}
