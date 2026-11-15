"""Voice I/O: Gemini transcription (with Groq fallback), Gemini TTS, and speakable text."""
import base64
import json

import httpx
import pytest

from app.config import Settings
from app.llm import providers
from app.llm.providers import GeminiProvider, GroqProvider, LLMRouter
from app.voice import service as voice
from app.voice.gemini import GeminiVoice
from app.voice.speech_text import to_speech

WAV = b"RIFF" + b"\0" * 64


class FakeVoiceAPIs:
    def __init__(self):
        self.gemini_bodies: list[dict] = []
        self.groq_calls = 0
        self.gemini_status = 200
        self.transcript = "Hey Nova, remind me to call Priya at 5 PM."

    def handler(self, req: httpx.Request) -> httpx.Response:
        if "generativelanguage" in req.url.host:
            body = json.loads(req.content)
            self.gemini_bodies.append(body)
            assert req.headers["x-goog-api-key"] == "g-key"
            if self.gemini_status != 200:
                return httpx.Response(self.gemini_status, json={"error": {"message": "down"}})
            if "transcribe" in body["model"]:
                return httpx.Response(200, json={"status": "completed", "steps": [
                    {"type": "user_input"}, {"type": "model_output", "content": [{"type": "text", "text": self.transcript}]}]})
            return httpx.Response(200, json={"status": "completed", "steps": [{"type": "model_output", "content": [
                {"type": "audio", "mime_type": "audio/wav", "data": base64.b64encode(WAV).decode()}]}]})
        self.groq_calls += 1
        return httpx.Response(200, json={"text": "remind me to call Priya at 5 PM"})


@pytest.fixture
def apis():
    fake = FakeVoiceAPIs()
    s = Settings(gemini_api_key="g-key", groq_api_key="q-key", llm_max_retries=0)
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    voice.set_gemini_voice(GeminiVoice(s, client))
    providers.set_router(LLMRouter(s, [GeminiProvider(s, client), GroqProvider(s, client)], sleep=lambda _: None))
    return fake


def test_transcribe_uses_gemini_smart_mode_with_accent_hints(client, auth, apis):
    r = client.post("/api/voice/transcribe", files={"audio": ("speech.wav", WAV, "audio/wav")}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "Hey Nova, remind me to call Priya at 5 PM.", "provider": "gemini", "fallback_from": []}
    body = apis.gemini_bodies[0]
    assert body["model"] == "models/gemini-3.5-transcribe" and body["store"] is False
    cfg = body["generation_config"]["transcription_config"]
    assert cfg["mode"] == "smart" and cfg["language_codes"] == ["en-IN", "en-US"]
    assert "NOVA" in cfg["custom_vocabulary"]
    assert body["input"][0] == {"type": "audio", "data": base64.b64encode(WAV).decode(), "mime_type": "audio/wav"}
    assert apis.groq_calls == 0


def test_transcribe_falls_back_to_groq_when_gemini_is_down(client, auth, apis):
    apis.gemini_status = 503
    r = client.post("/api/voice/transcribe", files={"audio": ("speech.wav", WAV, "audio/wav")}, headers=auth)
    assert r.status_code == 200
    assert r.json()["provider"] == "groq" and r.json()["fallback_from"] == ["gemini:unavailable"]


def test_silence_hallucinations_are_dropped(client, auth, apis):
    apis.transcript = "Thank you."
    r = client.post("/api/voice/transcribe", files={"audio": ("speech.wav", WAV, "audio/wav")}, headers=auth)
    assert r.json()["text"] == ""


def test_speak_returns_gemini_wav_and_spoken_text(client, auth, apis):
    r = client.post("/api/voice/speak", json={"text": "Done! I'll remind you **today at 5:00 PM**: Call Priya 📞", "voice": "Puck"}, headers=auth)
    assert r.status_code == 200, r.text
    out = r.json()
    assert base64.b64decode(out["audio"]) == WAV and out["mime"] == "audio/wav"
    assert out["spoken_text"] == "Done! I'll remind you today at 5:00 PM: Call Priya."
    body = apis.gemini_bodies[0]
    assert body["model"] == "gemini-3.8-flash-tts" and body["store"] is False
    assert body["generation_config"]["speech_config"] == [{"voice": "Puck"}]
    assert body["response_format"] == {"type": "audio", "mime_type": "audio/wav", "sample_rate": 24000}
    assert body["input"][0]["content"][0]["annotations"][0]["type"] == "speech_metadata"


def test_speak_rejects_unknown_voice_by_using_default(client, auth, apis):
    client.post("/api/voice/speak", json={"text": "Hello", "voice": "NotAVoice"}, headers=auth)
    assert apis.gemini_bodies[0]["generation_config"]["speech_config"] == [{"voice": "Kore"}]


def test_speak_without_key_is_503(client, auth):
    r = client.post("/api/voice/speak", json={"text": "Hello"}, headers=auth)
    assert r.status_code == 503


def test_speak_failure_is_502(client, auth, apis):
    apis.gemini_status = 500
    r = client.post("/api/voice/speak", json={"text": "Hello"}, headers=auth)
    assert r.status_code == 502


def test_voice_config(client, auth, apis):
    r = client.get("/api/voice/config", headers=auth).json()
    assert r["stt"] == ["gemini", "groq"] and r["tts"] is True and "Kore" in r["voices"] and len(r["voices"]) == 30


@pytest.mark.parametrize("text,spoken", [
    ("Here's what I can do:\n• Reminders — \"Remind me…\"\n• Tasks", "Here's what I can do: Reminders, Remind me…. Tasks."),
    ("Which one?\n1. Report.pdf\n2. Notes.pdf", "Which one? Option 1: Report.pdf. Option 2: Notes.pdf."),
    ("Saved https://github.com/langchain-ai/langgraph for you", "Saved github.com for you."),
    ("Use `npm run setup` then {\"a\": 1} done", "Use npm run setup then done."),
    ("Opening “LangGraph” (github.com).", "Opening LangGraph, github.com."),
])
def test_to_speech(text, spoken):
    assert to_speech(text) == spoken


def test_to_speech_shortens_long_answers():
    long = " ".join(f"Sentence number {i} is here." for i in range(60))
    out = to_speech(long)
    assert len(out) < 700 and out.endswith("The rest is on your screen.")


def test_tts_falls_back_to_lite_model_when_rate_limited_and_cools_down(client, auth):
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body["model"])
        if body["model"] == "gemini-3.8-flash-tts":
            return httpx.Response(429, json={"error": {"message": "Rate limit exceeded (limit: 3 requests per minute). Please retry in 10s"}})
        return httpx.Response(200, json={"status": "completed", "steps": [{"type": "model_output", "content": [
            {"type": "audio", "data": base64.b64encode(WAV).decode()}]}]})

    now = [1000.0]
    s = Settings(gemini_api_key="g-key")
    voice.set_gemini_voice(GeminiVoice(s, httpx.Client(transport=httpx.MockTransport(handler)), clock=lambda: now[0]))
    assert client.post("/api/voice/speak", json={"text": "Hi"}, headers=auth).status_code == 200
    assert calls == ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"]
    client.post("/api/voice/speak", json={"text": "Hi again"}, headers=auth)
    assert calls[2:] == ["gemini-3.8-flash-lite-tts"]  # main model skipped during its cooldown
    now[0] += 12  # Google said "retry in 10s"
    client.post("/api/voice/speak", json={"text": "Hi later"}, headers=auth)
    assert calls[3] == "gemini-3.8-flash-tts"


def test_live_previews_prefer_groq_to_save_gemini_quota(client, auth, apis):
    r = client.post("/api/voice/transcribe", data={"preview": "true"}, files={"audio": ("s.wav", WAV, "audio/wav")}, headers=auth)
    assert r.json()["provider"] == "groq" and apis.gemini_bodies == []
    r = client.post("/api/voice/transcribe", files={"audio": ("s.wav", WAV, "audio/wav")}, headers=auth)
    assert r.json()["provider"] == "gemini"  # the final transcript still uses Gemini


def test_rate_limited_gemini_stt_is_skipped_until_cooldown_ends(client, auth, apis):
    apis.gemini_status = 429
    client.post("/api/voice/transcribe", files={"audio": ("s.wav", WAV, "audio/wav")}, headers=auth)
    client.post("/api/voice/transcribe", files={"audio": ("s.wav", WAV, "audio/wav")}, headers=auth)
    assert len(apis.gemini_bodies) == 1 and apis.groq_calls == 2
