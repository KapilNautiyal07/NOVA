"""Speech-to-text and text-to-speech with provider fallback.

STT order comes from STT_PROVIDERS (default "gemini,groq"). A provider that fails with a
transient or quota error is skipped for the rest of the request; the response says which one
produced the transcript so the client can show it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..config import Settings, get_settings
from ..llm.providers import GroqProvider, ProviderError, get_router
from .gemini import GeminiVoice
from .speech_text import to_speech

log = logging.getLogger("nova.voice")

# Whisper-style models invent these on silence/noise; never treat them as a command.
_HALLUCINATIONS = re.compile(
    r"^(thanks? (you )?(for watching|so much)?|thank you\.?|you|bye\.?|\.+|subtitles by.*|♪+|\[.*\]|\(.*\))$", re.I
)


@dataclass
class Transcript:
    text: str
    provider: str
    fallback_from: list[str]


class VoiceUnavailable(Exception):
    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.status = status


_gemini: GeminiVoice | None = None


def gemini_voice(settings: Settings | None = None) -> GeminiVoice:
    global _gemini
    if _gemini is None:
        _gemini = GeminiVoice(settings or get_settings())
    return _gemini


def set_gemini_voice(v: GeminiVoice | None) -> None:
    """Used by tests to inject a mocked transport."""
    global _gemini
    _gemini = v


def clean_transcript(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return "" if _HALLUCINATIONS.match(t) else t


def transcribe(audio: bytes, filename: str, mime: str, *, vocabulary: list[str] | None = None, preview: bool = False) -> Transcript:
    """`preview=True` is the live caption while the user is still talking: it prefers the fast
    provider (Groq) so captions don't use up Gemini's quota; the final transcript prefers Gemini."""
    settings = get_settings()
    order = [p.strip() for p in settings.stt_providers.split(",") if p.strip()]
    if preview and "groq" in order:
        order = ["groq"] + [p for p in order if p != "groq"]
    errors: list[str] = []
    configured_any = False
    for name in order:
        try:
            if name == "gemini":
                g = gemini_voice(settings)
                if not g.configured():
                    continue
                configured_any = True
                text = g.transcribe(audio, mime, vocabulary=vocabulary)
            elif name == "groq":
                p = get_router().providers.get("groq")
                if not isinstance(p, GroqProvider) or not p.configured():
                    continue
                configured_any = True
                text = p.transcribe(audio, filename, mime)
            else:
                continue
            return Transcript(clean_transcript(text), name, errors)
        except ProviderError as exc:
            log.warning("STT provider %s failed: %s", name, exc)
            errors.append(f"{name}:{exc.kind}")
    if not configured_any:
        raise VoiceUnavailable("Voice transcription needs GEMINI_API_KEY or GROQ_API_KEY on the NOVA server.", 503)
    raise VoiceUnavailable(f"Transcription failed ({', '.join(errors)}). Please try again or type.", 502)


def synthesize(text: str, *, voice: str | None = None) -> tuple[bytes, str]:
    """Returns (wav_bytes, spoken_text). Raises VoiceUnavailable when TTS can't be produced."""
    spoken = to_speech(text)
    if not spoken:
        raise VoiceUnavailable("Nothing to say.", 400)
    g = gemini_voice()
    if not g.configured():
        raise VoiceUnavailable("Natural voice needs GEMINI_API_KEY on the NOVA server.", 503)
    try:
        return g.synthesize(spoken, voice=voice), spoken
    except ProviderError as exc:
        log.warning("TTS failed: %s", exc)
        raise VoiceUnavailable(f"Speech synthesis failed ({exc.kind}).", 502) from exc
