package com.jarvis.assistant.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class WakeWordTest {
    @Test fun commandsAfterWakeWord() {
        assertEquals("open YouTube", WakeWord.command("Jarvis open YouTube"))
        assertEquals("what time is it", WakeWord.command("hey jarvis, what time is it"))
        assertEquals("set a timer for 5 minutes", WakeWord.command("OK Jarvis set a timer for 5 minutes"))
        assertEquals("", WakeWord.command("Jarvis"))
    }

    @Test fun otherSpeechIsIgnored() {
        assertNull(WakeWord.command("open YouTube"))
        assertNull(WakeWord.command("I told Jarvis about it")) // must be addressed first
        assertNull(WakeWord.command("jarvisland trip"))
    }
}
