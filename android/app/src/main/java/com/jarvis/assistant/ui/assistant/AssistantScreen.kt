package com.jarvis.assistant.ui.assistant

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
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
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleEventEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.jarvis.assistant.data.ChatMessage
import com.jarvis.assistant.ui.components.ActionChip
import com.jarvis.assistant.ui.components.MicIcon
import com.jarvis.assistant.ui.components.ScreenHeader
import com.jarvis.assistant.ui.components.StatusPill
import com.jarvis.assistant.ui.components.statusColor
import com.jarvis.assistant.ui.theme.Amber
import com.jarvis.assistant.ui.theme.Cyan
import com.jarvis.assistant.ui.theme.Danger
import com.jarvis.assistant.ui.theme.Navy
import com.jarvis.assistant.ui.theme.Violet
import com.jarvis.assistant.voice.VoicePhase

@Composable
fun AssistantScreen(vm: AssistantViewModel = viewModel()) {
    val state by vm.uiState.collectAsStateWithLifecycle()
    val context = LocalContext.current
    val micPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) vm.onMicClick()
        else vm.showNotice("Microphone permission denied. You can still type, or allow it in App Access.")
    }
    LifecycleEventEffect(Lifecycle.Event.ON_START) { vm.onForeground() }
    LifecycleEventEffect(Lifecycle.Event.ON_STOP) { vm.onBackground() }
    val onMic = {
        if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            vm.onMicClick()
        } else {
            micPermission.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp)) {
        ScreenHeader("J.A.R.V.I.S.", state.serverUrl) {
            IconButton(onClick = vm::newConversation) {
                Icon(Icons.Filled.Refresh, "New conversation", tint = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }

        val handsFree by vm.handsFree.collectAsStateWithLifecycle()
        HandsFreeBar(handsFree)

        val listState = rememberLazyListState()
        val itemCount = state.messages.size + if (state.plan != null) 1 else 0
        LaunchedEffect(itemCount, state.plan?.status) {
            if (itemCount > 0) listState.animateScrollToItem(itemCount - 1)
        }
        LazyColumn(
            state = listState,
            modifier = Modifier.weight(1f).fillMaxWidth(),
            contentPadding = PaddingValues(vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            itemsIndexed(state.messages) { _, msg -> MessageBubble(msg) }
            state.plan?.let { plan ->
                item(key = "plan-${plan.id}") {
                    PlanCard(
                        plan = plan,
                        busy = state.planBusy,
                        onApprove = vm::approvePlan,
                        onCancel = vm::cancelPlan,
                        onResume = vm::resumePlan,
                        onRetry = vm::retryStep,
                        onDismiss = vm::dismissPlan,
                    )
                }
            }
        }

        state.confirmation?.let { ConfirmStepDialog(it, onConfirm = vm::confirmStep, onDecline = vm::declineStep) }
        state.verification?.let { VerifyStepDialog(it, onAnswer = vm::verifyStep) }

        AnimatedVisibility(visible = state.notice != null, enter = fadeIn(), exit = fadeOut()) {
            NoticeBar(state.notice.orEmpty(), onDismiss = vm::dismissNotice)
        }

        Column(Modifier.fillMaxWidth().padding(vertical = 8.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            ArcReactor(phase = state.phase, level = state.level, onClick = onMic, modifier = Modifier.size(132.dp))
            Spacer(Modifier.height(8.dp))
            Text(
                text = state.partial.ifBlank { phaseLabel(state.phase, state.voiceAvailable) },
                style = MaterialTheme.typography.bodyMedium,
                color = phaseColor(state.phase),
                maxLines = 2,
            )
        }

        CommandInput(enabled = state.phase != VoicePhase.THINKING, onSend = vm::send)
        Spacer(Modifier.height(8.dp))
    }
}

@Composable
private fun MessageBubble(msg: ChatMessage) {
    val user = msg.fromUser
    Box(Modifier.fillMaxWidth(), contentAlignment = if (user) Alignment.CenterEnd else Alignment.CenterStart) {
        Surface(
            shape = RoundedCornerShape(
                topStart = 18.dp, topEnd = 18.dp,
                bottomStart = if (user) 18.dp else 4.dp, bottomEnd = if (user) 4.dp else 18.dp,
            ),
            color = if (user) MaterialTheme.colorScheme.secondaryContainer else MaterialTheme.colorScheme.surfaceContainer,
            border = if (user) null else androidx.compose.foundation.BorderStroke(1.dp, Cyan.copy(alpha = 0.18f)),
            modifier = Modifier.widthIn(max = 300.dp),
        ) {
            Column(Modifier.padding(horizontal = 14.dp, vertical = 10.dp)) {
                Text(
                    msg.text,
                    style = MaterialTheme.typography.bodyLarge,
                    color = if (user) MaterialTheme.colorScheme.onSecondaryContainer else MaterialTheme.colorScheme.onSurface,
                )
                msg.action?.let { action ->
                    Spacer(Modifier.height(8.dp))
                    ActionChip(action.type, action.value)
                }
                msg.task?.let { task ->
                    Spacer(Modifier.height(6.dp))
                    StatusPill(task.status.name, statusColor(task.status))
                }
            }
        }
    }
}

@Composable
private fun NoticeBar(text: String, onDismiss: () -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .background(Amber.copy(alpha = 0.12f), RoundedCornerShape(12.dp))
            .padding(start = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(text, color = Amber, style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(1f))
        IconButton(onClick = onDismiss) { Icon(Icons.Filled.Close, "Dismiss", tint = Amber) }
    }
}

@Composable
private fun CommandInput(enabled: Boolean, onSend: (String) -> Unit) {
    var text by rememberSaveable { mutableStateOf("") }
    val submit = {
        if (text.isNotBlank()) {
            onSend(text)
            text = ""
        }
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
        OutlinedTextField(
            value = text,
            onValueChange = { text = it },
            placeholder = { Text("Type a command…") },
            singleLine = true,
            shape = RoundedCornerShape(24.dp),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
            keyboardActions = KeyboardActions(onSend = { submit() }),
            colors = OutlinedTextFieldDefaults.colors(
                unfocusedContainerColor = MaterialTheme.colorScheme.surfaceContainer,
                focusedContainerColor = MaterialTheme.colorScheme.surfaceContainer,
                unfocusedBorderColor = MaterialTheme.colorScheme.outline,
            ),
            modifier = Modifier.weight(1f),
        )
        Spacer(Modifier.size(8.dp))
        FilledIconButton(
            onClick = submit,
            enabled = enabled && text.isNotBlank(),
            colors = IconButtonDefaults.filledIconButtonColors(containerColor = Cyan, contentColor = Navy),
            modifier = Modifier.size(52.dp),
        ) { Icon(Icons.AutoMirrored.Filled.Send, "Send") }
    }
}

private fun phaseLabel(phase: VoicePhase, voiceAvailable: Boolean) = when (phase) {
    VoicePhase.IDLE -> if (voiceAvailable) "Tap the core to speak" else "Voice input unavailable — type below"
    VoicePhase.LISTENING -> "Listening…"
    VoicePhase.THINKING -> "Thinking…"
    VoicePhase.SPEAKING -> "Speaking — tap to stop"
}

private fun phaseColor(phase: VoicePhase): Color = when (phase) {
    VoicePhase.IDLE -> Cyan
    VoicePhase.LISTENING -> Danger
    VoicePhase.THINKING -> Violet
    VoicePhase.SPEAKING -> Color(0xFF7DD3FC)
}

/** Animated arc-reactor style orb that reacts to voice phase and mic level. */
@Composable
fun ArcReactor(phase: VoicePhase, level: Float, onClick: () -> Unit, modifier: Modifier = Modifier) {
    val transition = rememberInfiniteTransition(label = "reactor")
    val rotation by transition.animateFloat(
        initialValue = 0f, targetValue = 360f,
        animationSpec = infiniteRepeatable(tween(5000, easing = LinearEasing)), label = "rotation",
    )
    val pulse by transition.animateFloat(
        initialValue = 0.92f, targetValue = 1.06f,
        animationSpec = infiniteRepeatable(tween(1300), RepeatMode.Reverse), label = "pulse",
    )
    val animLevel by animateFloatAsState(level, label = "level")
    val color by animateColorAsState(phaseColor(phase), label = "color")
    val spin = if (phase == VoicePhase.THINKING) 3f else 1f

    Box(
        modifier
            .clip(CircleShape)
            .clickable(interactionSource = remember { MutableInteractionSource() }, indication = null, onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Canvas(Modifier.fillMaxSize()) {
            val r = size.minDimension / 2f
            val core = when (phase) {
                VoicePhase.LISTENING -> 0.85f + 0.35f * animLevel
                VoicePhase.IDLE -> pulse
                else -> pulse * 1.04f
            }
            drawCircle(Brush.radialGradient(listOf(color.copy(alpha = 0.35f), Color.Transparent), center, r), r)

            fun ring(radius: Float, width: Float, alpha: Float, start: Float, sweep: Float, count: Int) {
                val gap = 360f / count
                repeat(count) { i ->
                    drawArc(
                        color = color.copy(alpha = alpha),
                        startAngle = start + i * gap,
                        sweepAngle = sweep,
                        useCenter = false,
                        topLeft = Offset(center.x - radius, center.y - radius),
                        size = Size(radius * 2, radius * 2),
                        style = Stroke(width, cap = StrokeCap.Round),
                    )
                }
            }
            rotate(rotation * spin) { ring(r * 0.86f, 3.dp.toPx(), 0.9f, 0f, 70f, 3) }
            rotate(-rotation * spin * 1.6f) { ring(r * 0.70f, 2.dp.toPx(), 0.55f, 20f, 28f, 6) }
            drawCircle(color.copy(alpha = 0.25f), radius = r * 0.52f, style = Stroke(1.dp.toPx()))
            drawCircle(
                Brush.radialGradient(listOf(Color.White, color, color.copy(alpha = 0.2f)), center, r * 0.42f * core),
                radius = r * 0.42f * core,
            )
        }
        Icon(MicIcon, contentDescription = "Talk", tint = Navy.copy(alpha = 0.85f), modifier = Modifier.size(30.dp))
    }
}
