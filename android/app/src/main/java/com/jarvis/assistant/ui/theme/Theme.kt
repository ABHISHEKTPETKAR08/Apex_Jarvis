package com.jarvis.assistant.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

val Cyan = Color(0xFF00E5FF)
val Navy = Color(0xFF05080F)
val Panel = Color(0xFF0C1320)
val PanelHigh = Color(0xFF142033)
val Outline = Color(0xFF22324A)
val TextPrimary = Color(0xFFE6F1FF)
val TextMuted = Color(0xFF8DA2BD)
val Success = Color(0xFF4ADE80)
val Danger = Color(0xFFFF5370)
val Amber = Color(0xFFFFC857)
val Violet = Color(0xFFB388FF)

private val JarvisColors = darkColorScheme(
    primary = Cyan,
    onPrimary = Navy,
    primaryContainer = Color(0xFF003944),
    onPrimaryContainer = Cyan,
    secondary = Color(0xFF7DD3FC),
    onSecondary = Navy,
    secondaryContainer = Color(0xFF0E2A3A),
    onSecondaryContainer = Color(0xFFBDE9FF),
    tertiary = Violet,
    background = Navy,
    onBackground = TextPrimary,
    surface = Navy,
    onSurface = TextPrimary,
    surfaceVariant = PanelHigh,
    onSurfaceVariant = TextMuted,
    surfaceContainerLowest = Color(0xFF03060B),
    surfaceContainerLow = Color(0xFF090F19),
    surfaceContainer = Panel,
    surfaceContainerHigh = PanelHigh,
    surfaceContainerHighest = Color(0xFF1A2940),
    outline = Outline,
    outlineVariant = Color(0xFF18263A),
    error = Danger,
    onError = Navy,
)

private val base = Typography()
private val JarvisType = base.copy(
    headlineSmall = base.headlineSmall.copy(fontWeight = FontWeight.Bold, letterSpacing = 2.sp),
    titleLarge = base.titleLarge.copy(fontWeight = FontWeight.SemiBold),
    titleMedium = base.titleMedium.copy(fontWeight = FontWeight.SemiBold),
    labelSmall = TextStyle(fontSize = 11.sp, fontWeight = FontWeight.Medium, letterSpacing = 1.2.sp),
)

@Composable
fun JarvisTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = JarvisColors, typography = JarvisType, content = content)
}
