package com.jarvis.assistant.voice

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.util.Locale

enum class VoicePhase { IDLE, LISTENING, THINKING, SPEAKING }

/** Wraps Android SpeechRecognizer + TextToSpeech and exposes their state as StateFlows. Create on the main thread. */
class VoiceEngine(private val context: Context) : RecognitionListener, TextToSpeech.OnInitListener {
    private val _phase = MutableStateFlow(VoicePhase.IDLE)
    val phase: StateFlow<VoicePhase> = _phase.asStateFlow()

    /** Normalized microphone level 0..1 while listening. */
    private val _level = MutableStateFlow(0f)
    val level: StateFlow<Float> = _level.asStateFlow()

    private val _partial = MutableStateFlow("")
    val partial: StateFlow<String> = _partial.asStateFlow()

    var onResult: ((String) -> Unit)? = null
    var onError: ((String) -> Unit)? = null
    var onErrorCode: ((Int) -> Unit)? = null // raw SpeechRecognizer error, for callers that retry

    val recognitionAvailable: Boolean = SpeechRecognizer.isRecognitionAvailable(context)

    private var recognizer: SpeechRecognizer? = null
    private var player: android.media.MediaPlayer? = null
    private var tts: TextToSpeech? = null

    init {
        tts = TextToSpeech(context, this)
    }

    override fun onInit(status: Int) {
        if (status != TextToSpeech.SUCCESS) {
            onError?.invoke("Text-to-speech isn't available on this device; replies will be shown as text.")
            return
        }
        val uk = tts?.setLanguage(Locale.UK) ?: TextToSpeech.ERROR
        if (uk == TextToSpeech.LANG_MISSING_DATA || uk == TextToSpeech.LANG_NOT_SUPPORTED) {
            val fallback = tts?.setLanguage(Locale.getDefault()) ?: TextToSpeech.ERROR
            if (fallback == TextToSpeech.LANG_MISSING_DATA || fallback == TextToSpeech.LANG_NOT_SUPPORTED) {
                onError?.invoke("No text-to-speech voice is installed (Settings > Text-to-speech).")
            }
        }
        tts?.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) { _phase.value = VoicePhase.SPEAKING }
            override fun onDone(utteranceId: String?) { if (_phase.value == VoicePhase.SPEAKING) _phase.value = VoicePhase.IDLE }
            @Deprecated("Deprecated in Java")
            override fun onError(utteranceId: String?) { _phase.value = VoicePhase.IDLE }
        })
    }

    fun startListening() {
        if (!recognitionAvailable) {
            onError?.invoke("Speech recognition isn't available on this device.")
            return
        }
        tts?.stop()
        val rec = recognizer ?: SpeechRecognizer.createSpeechRecognizer(context).also {
            it.setRecognitionListener(this)
            recognizer = it
        }
        _partial.value = ""
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault().toLanguageTag())
            .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
        rec.startListening(intent)
        _phase.value = VoicePhase.LISTENING
    }

    fun stopListening() {
        recognizer?.stopListening()
    }

    /** Stops listening and discards audio, e.g. when the app leaves the screen. */
    fun cancelListening() {
        if (_phase.value == VoicePhase.LISTENING) {
            recognizer?.cancel()
            finishListening()
            _partial.value = ""
        }
    }

    fun speak(text: String) {
        tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "jarvis-${System.nanoTime()}")
    }

    /** Plays server-synthesized speech (Azure). Calls onFailed if the file can't be played. */
    fun playAudio(file: java.io.File, onFailed: () -> Unit) {
        tts?.stop()
        player?.release()
        player = try {
            android.media.MediaPlayer().apply {
                setDataSource(file.absolutePath)
                setOnCompletionListener {
                    if (_phase.value == VoicePhase.SPEAKING) _phase.value = VoicePhase.IDLE
                    it.release()
                    if (player === it) player = null
                }
                prepare()
                start()
            }
        } catch (e: Exception) {
            onFailed()
            null
        }
        if (player != null) _phase.value = VoicePhase.SPEAKING
    }

    fun stopSpeaking() {
        player?.let { runCatching { it.stop() }; it.release() }
        player = null
        tts?.stop()
        _phase.value = VoicePhase.IDLE
    }

    fun release() {
        recognizer?.destroy()
        recognizer = null
        tts?.shutdown()
        tts = null
        player?.release()
        player = null
    }

    private fun finishListening() {
        _level.value = 0f
        if (_phase.value == VoicePhase.LISTENING) _phase.value = VoicePhase.IDLE
    }

    override fun onResults(results: Bundle?) {
        finishListening()
        val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()
        _partial.value = ""
        if (text.isNullOrBlank()) onError?.invoke("I didn't catch that.") else onResult?.invoke(text)
    }

    override fun onPartialResults(partialResults: Bundle?) {
        partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()?.let { _partial.value = it }
    }

    override fun onError(error: Int) {
        finishListening()
        _partial.value = ""
        onErrorCode?.invoke(error)
        onError?.invoke(
            when (error) {
                SpeechRecognizer.ERROR_NO_MATCH, SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "I didn't catch that."
                SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "Microphone permission is required."
                SpeechRecognizer.ERROR_NETWORK, SpeechRecognizer.ERROR_NETWORK_TIMEOUT -> "Speech service network error."
                SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "Speech recognizer is busy, try again."
                else -> "Speech error ($error)."
            },
        )
    }

    override fun onRmsChanged(rmsdB: Float) {
        _level.value = ((rmsdB + 2f) / 12f).coerceIn(0f, 1f)
    }

    override fun onEndOfSpeech() { _level.value = 0f }
    override fun onReadyForSpeech(params: Bundle?) {}
    override fun onBeginningOfSpeech() {}
    override fun onBufferReceived(buffer: ByteArray?) {}
    override fun onEvent(eventType: Int, params: Bundle?) {}
}
