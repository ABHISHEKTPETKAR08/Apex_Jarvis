package com.jarvis.assistant.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AppResolverTest {
    // Mirrors the test phone: YouTube Music listed before YouTube.
    private val apps = listOf(
        AppEntry("YouTube Music", "com.google.android.apps.youtube.music"),
        AppEntry("YouTube", "com.google.android.youtube"),
        AppEntry("Among Us", "com.innersloth.spacemafia"),
        AppEntry("WhatsApp", "com.whatsapp"),
        AppEntry("Camera", "com.android.camera"),
        AppEntry("Calculator", "com.miui.calculator"),
    )

    private fun pkg(q: String) = AppResolver.resolve(q, apps)?.packageName

    @Test fun exactLabelsIgnoringCaseAndSpacing() {
        assertEquals("com.google.android.youtube", pkg("YouTube"))
        assertEquals("com.google.android.youtube", pkg("youtube"))
        assertEquals("com.innersloth.spacemafia", pkg("Among Us"))
        assertEquals("com.innersloth.spacemafia", pkg("among us"))
        assertEquals("com.innersloth.spacemafia", pkg("AmongUs"))
    }

    @Test fun youtubeIsNotConfusedWithYoutubeMusic() {
        assertEquals("com.google.android.youtube", pkg("the YouTube app"))
        assertEquals("com.google.android.apps.youtube.music", pkg("YouTube Music"))
        assertEquals("com.google.android.apps.youtube.music", pkg("yt music"))
    }

    @Test fun aliasesAndPackageNames() {
        assertEquals("com.google.android.youtube", pkg("yt"))
        assertEquals("com.google.android.youtube", pkg("com.google.android.youtube"))
        assertEquals("com.whatsapp", pkg("whatsapp app")) // "app" filler must not break "whatsapp"
        assertEquals("com.miui.calculator", pkg("calc"))
    }

    @Test fun missingAppsAreNotGuessed() {
        assertNull(pkg("Instagram")) // alias exists, app not installed
        assertNull(pkg("Minecraft"))
        assertNull(pkg("a"))
        assertNull(pkg("   "))
        assertTrue(AppResolver.decide("Minecraft", apps, emptySet()) is LaunchDecision.NotInstalled)
    }

    @Test fun blockedAppsAreReportedNotLaunched() {
        val d = AppResolver.decide("YouTube", apps, setOf("com.google.android.youtube"))
        assertTrue(d is LaunchDecision.Blocked)
        assertEquals("YouTube", (d as LaunchDecision.Blocked).app.label)
        // Blocking YouTube doesn't block YouTube Music.
        assertTrue(AppResolver.decide("YouTube Music", apps, setOf("com.google.android.youtube")) is LaunchDecision.Launch)
    }
}
