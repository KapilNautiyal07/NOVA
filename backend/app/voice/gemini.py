"""Gemini speech models via the Interactions API (REST, no SDK — same style as llm/providers.py).

* Transcription: gemini-3.5-transcribe in "smart" mode (drops fillers like "um", handles
  self-corrections such as "at 5, no, 6 PM", formats numbers/times), with language hints
  (en-IN, en-US by default) and a custom vocabulary so names like "NOVA" come out right.
* Speech: gemini-3.8-flash-tts, 24 kHz mono WAV, a prebuilt voice and a speaking-style prompt.

Requests set store=false so Google does not keep the interaction.
"""
from __future__ import annotations

import base64
import logging
import re
import time

import httpx

from ..config import Settings
from ..llm.providers import ProviderError, _classify_http

log = logging.getLogger("nova.voice")

# Prebuilt Gemini TTS voices (https://ai.google.dev/gemini-api/docs/speech-generation).
VOICES = [
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe",
    "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi",
    "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird",
    "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
]


def _csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


class GeminiVoice:
    name = "gemini"

    def __init__(self, settings: Settings, client: httpx.Client | None = None, clock=time.monotonic):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.voice_timeout_seconds)
        self._clock = clock
        self._cooldown: dict[str, float] = {}  # model → don't call before this time (after a 429)

    def configured(self) -> bool:
        return bool(self.settings.gemini_api_key)

    def cooling_down(self, model: str) -> bool:
        return self._cooldown.get(model, 0) > self._clock()

    def _note_rate_limit(self, model: str, err: ProviderError) -> None:
        m = re.search(r"retry in (\d+(?:\.\d+)?)\s*s", err.detail or "", re.I)
        self._cooldown[model] = self._clock() + (float(m.group(1)) + 1 if m else 60.0)

    @property
    def _url(self) -> str:
        return f"{self.settings.gemini_base_url.rstrip('/')}/interactions"

    def _post(self, payload: dict) -> dict:
        if not self.configured():
            raise ProviderError(self.name, "not_configured")
        try:
            resp = self.client.post(self._url, json=payload, headers={"x-goog-api-key": self.settings.gemini_api_key or ""})
        except httpx.TimeoutException as exc:
            raise ProviderError(self.name, "timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, "unavailable", type(exc).__name__) from exc
        if resp.status_code != 200:
            err = _classify_http(self.name, resp)
            if err.kind == "rate_limit":
                err.detail = resp.text[:300]  # keeps Google's "retry in Ns" hint
            raise err
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(self.name, "bad_response", "invalid JSON body") from exc
        if data.get("status") not in (None, "completed"):
            raise ProviderError(self.name, "bad_response", f"status={data.get('status')}")
        return data

    @staticmethod
    def _outputs(data: dict, kind: str) -> list[dict]:
        return [
            c
            for step in data.get("steps") or []
            if step.get("type") == "model_output"
            for c in step.get("content") or []
            if c.get("type") == kind
        ]

    # ------------------------------------------------------------------ speech → text
    def transcribe(self, audio: bytes, mime: str, *, vocabulary: list[str] | None = None) -> str:
        model = self.settings.gemini_stt_model
        if self.cooling_down(model):
            raise ProviderError(self.name, "rate_limit", "cooling down")
        try:
            return self._transcribe(audio, mime, vocabulary)
        except ProviderError as err:
            if err.kind == "rate_limit":
                self._note_rate_limit(model, err)
            raise

    def _transcribe(self, audio: bytes, mime: str, vocabulary: list[str] | None) -> str:
        config: dict = {"mode": "smart"}
        langs = _csv(self.settings.stt_language_codes)
        if langs:
            config["language_codes"] = langs
        vocab = list(dict.fromkeys(_csv(self.settings.stt_vocabulary) + (vocabulary or [])))[:100]
        if vocab:
            config["custom_vocabulary"] = vocab
        data = self._post(
            {
                "model": f"models/{self.settings.gemini_stt_model}",
                "store": False,
                "input": [{"type": "audio", "data": base64.b64encode(audio).decode(), "mime_type": mime}],
                "generation_config": {"transcription_config": config},
            }
        )
        return " ".join(c.get("text", "") for c in self._outputs(data, "text")).strip()

    # ------------------------------------------------------------------ text → speech
    def synthesize(self, text: str, *, voice: str | None = None, style: str | None = None) -> bytes:
        """Tries the main TTS model, then the lighter one when the first is rate-limited/down."""
        models = [m for m in (self.settings.gemini_tts_model, self.settings.gemini_tts_fallback_model) if m]
        last: ProviderError | None = None
        for model in dict.fromkeys(models):
            if self.cooling_down(model):
                last = ProviderError(self.name, "rate_limit", "cooling down")
                continue
            try:
                return self._synthesize(model, text, voice, style)
            except ProviderError as err:
                last = err
                if err.kind == "rate_limit":
                    self._note_rate_limit(model, err)
                if err.kind not in ("rate_limit", "unavailable", "timeout"):
                    raise
                log.info("TTS model %s unavailable (%s); trying the next one", model, err.kind)
        raise last or ProviderError(self.name, "not_configured")

    def _synthesize(self, model: str, text: str, voice: str | None, style: str | None) -> bytes:
        voice = voice if voice in VOICES else self.settings.tts_default_voice
        data = self._post(
            {
                "model": model,
                "store": False,
                "input": [
                    {
                        "type": "user_input",
                        "content": [
                            {
                                "type": "text",
                                "text": text,
                                "annotations": [{"type": "speech_metadata", "style": style or self.settings.tts_style}],
                            }
                        ],
                    }
                ],
                "response_format": {"type": "audio", "mime_type": "audio/wav", "sample_rate": 24000},
                "generation_config": {"speech_config": [{"voice": voice}]},
            }
        )
        audio = self._outputs(data, "audio")
        if not audio or not audio[-1].get("data"):
            raise ProviderError(self.name, "bad_response", "no audio in response")
        return base64.b64decode(audio[-1]["data"])
