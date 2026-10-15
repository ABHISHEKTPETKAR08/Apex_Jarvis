import struct

import pytest
from fastapi.testclient import TestClient

from fake_ms import FakeMicrosoft
from jarvis_server import create_app

ENV = {"AI_PROVIDER": "offline", "AZURE_SPEECH_KEY": "speech-key", "AZURE_SPEECH_REGION": "centralindia"}


def wav(seconds=1):
    data = b"\x00\x00" * 16000 * seconds
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) + b"data" + struct.pack("<I", len(data))
    return header + data


@pytest.fixture
def fake():
    return FakeMicrosoft()


def make(tmp_path, fake, env):
    return TestClient(create_app(env=env, data_dir=tmp_path, http=fake.client()))


def test_not_configured_returns_not_configured(tmp_path, fake):
    c = make(tmp_path, fake, {"AI_PROVIDER": "offline"})
    assert c.get("/integrations/status").json()["azure_speech"]["state"] == "NOT_CONFIGURED"
    r = c.post("/speech/tts", json={"text": "hello"})
    assert r.status_code == 503 and r.json()["code"] == "NOT_CONFIGURED"
    r = c.post("/speech/stt", content=wav())
    assert r.status_code == 503 and r.json()["code"] == "NOT_CONFIGURED"
    assert fake.calls == []


def test_bad_region_is_not_configured(tmp_path, fake):
    c = make(tmp_path, fake, {**ENV, "AZURE_SPEECH_REGION": "https://evil.example"})
    assert c.post("/speech/tts", json={"text": "hi"}).json()["code"] == "NOT_CONFIGURED"


def test_tts_needs_speaker_grant_escapes_ssml_and_hides_key(tmp_path, fake):
    c = make(tmp_path, fake, ENV)
    r = c.post("/speech/tts", json={"text": "hi"})
    assert r.status_code == 403 and r.json()["code"] == "PERMISSION_DENIED" and fake.calls == []
    c.post("/permissions/grant", json={"capability": "speaker"})
    r = c.post("/speech/tts", json={"text": "Tom & Jerry <break time='9s'/>"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and r.content == b"ID3fake-mp3"
    assert "Tom &amp; Jerry &lt;break" in fake.last_ssml and "en-GB-RyanNeural" in fake.last_ssml
    assert "speech-key" not in r.text
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "speech_tts" in audit and "Jerry" not in audit


def test_stt_needs_mic_grant_and_validates_audio(tmp_path, fake):
    c = make(tmp_path, fake, ENV)
    assert c.post("/speech/stt", content=wav()).status_code == 403
    c.post("/permissions/grant", json={"capability": "microphone"})
    r = c.post("/speech/stt?language=en-US", content=wav())
    assert r.status_code == 200 and r.json() == {"text": "Open YouTube."} and fake.last_stt_language == "en-US"
    r = c.post("/speech/stt", content=b"not audio")
    assert r.status_code == 400 and r.json()["code"] == "BAD_INPUT"
    assert c.post("/speech/stt", content=wav(70)).status_code == 400
    assert not list(tmp_path.glob("*.wav"))


def test_wrong_key_is_auth_failure(tmp_path, fake):
    c = make(tmp_path, fake, {**ENV, "AZURE_SPEECH_KEY": "bad-key-7Q9x"})
    c.post("/permissions/grant", json={"capability": "speaker"})
    r = c.post("/speech/tts", json={"text": "hi"})
    assert r.status_code == 502 and r.json()["code"] == "AUTH_FAILED" and "bad-key-7Q9x" not in r.text
