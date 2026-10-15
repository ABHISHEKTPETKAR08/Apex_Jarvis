"""Optional Azure AI Speech (REST): neural text-to-speech and short-audio speech-to-text.

The subscription key stays on this server; the phone only receives audio or text. Native Android speech
keeps working without this.
"""
import re
from typing import Mapping, Optional
from xml.sax.saxutils import escape

import httpx

from graph import IntegrationError, http_call

MAX_TTS_CHARS = 1000
MAX_STT_BYTES = 2 * 1024 * 1024  # ~60 s of 16 kHz mono PCM


def _clean(env: Mapping[str, str], key: str) -> str:
    v = (env.get(key) or "").strip()
    return "" if not v or v.startswith("<") else v


class AzureSpeech:
    def __init__(self, env: Mapping[str, str], http: Optional[httpx.Client] = None):
        self.key = _clean(env, "AZURE_SPEECH_KEY")
        self.region = _clean(env, "AZURE_SPEECH_REGION").lower()
        self.voice = _clean(env, "AZURE_SPEECH_VOICE") or "en-GB-RyanNeural"
        self.language = _clean(env, "AZURE_SPEECH_LANGUAGE") or "en-IN"
        self.http = http or httpx.Client(timeout=20.0)
        self.config_error = None
        if self.region and not re.fullmatch(r"[a-z0-9]+", self.region):
            self.config_error = "AZURE_SPEECH_REGION should look like 'centralindia' or 'eastus'"

    @property
    def configured(self) -> bool:
        return bool(self.key and self.region and not self.config_error)

    def status(self) -> dict:
        if not self.configured:
            missing = [k for k, v in (("AZURE_SPEECH_KEY", self.key), ("AZURE_SPEECH_REGION", self.region)) if not v]
            detail = self.config_error or f"Set {', '.join(missing)}; the phone's own speech is used meanwhile"
            return {"state": "NOT_CONFIGURED", "detail": detail}
        return {"state": "CONFIGURED", "region": self.region, "voice": self.voice, "language": self.language}

    def _check(self, resp: httpx.Response, what: str):
        if resp.status_code < 400:
            return
        if resp.status_code in (401, 403):
            raise IntegrationError("AUTH_FAILED", f"{what}: Azure Speech key or region is wrong")
        if resp.status_code == 429:
            raise IntegrationError("RATE_LIMITED", f"{what}: Azure Speech quota/rate limit", transient=True)
        if resp.status_code >= 500:
            raise IntegrationError("PROVIDER_ERROR", f"{what}: Azure Speech error {resp.status_code}", transient=True)
        raise IntegrationError("PROVIDER_ERROR", f"{what}: rejected ({resp.status_code})")

    def tts(self, text: str) -> bytes:
        if not self.configured:
            raise IntegrationError("NOT_CONFIGURED", self.status()["detail"])
        text = text.strip()[:MAX_TTS_CHARS]
        ssml = (f"<speak version='1.0' xml:lang='{escape(self.language)}'>"
                f"<voice name='{escape(self.voice)}'>{escape(text)}</voice></speak>")
        resp = http_call(self.http, "POST", f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1",
                         content=ssml.encode("utf-8"), headers={
                             "Ocp-Apim-Subscription-Key": self.key,
                             "Content-Type": "application/ssml+xml",
                             "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
                             "User-Agent": "jarvis-server"})
        self._check(resp, "Text-to-speech")
        return resp.content

    def stt(self, wav: bytes, language: Optional[str] = None) -> str:
        if not self.configured:
            raise IntegrationError("NOT_CONFIGURED", self.status()["detail"])
        if len(wav) > MAX_STT_BYTES:
            raise IntegrationError("BAD_INPUT", "Audio is too long (max about 60 seconds)")
        if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
            raise IntegrationError("BAD_INPUT", "Audio must be WAV (16 kHz mono PCM)")
        lang = language if language and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", language) else self.language
        resp = http_call(
            self.http, "POST",
            f"https://{self.region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1",
            params={"language": lang, "format": "simple"}, content=wav,
            headers={"Ocp-Apim-Subscription-Key": self.key,
                     "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000"})
        self._check(resp, "Speech-to-text")
        body = resp.json()
        status = body.get("RecognitionStatus")
        if status == "Success":
            return body.get("DisplayText", "")
        if status in ("NoMatch", "InitialSilenceTimeout", "BabbleTimeout"):
            return ""
        raise IntegrationError("PROVIDER_ERROR", f"Speech-to-text failed ({status})")
