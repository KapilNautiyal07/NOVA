"""Unified LLM interface with automatic provider fallback (Gemini → Groq by default).

* API keys are read from server-side environment variables only.
* Transient failures (timeouts, 5xx) are retried with exponential backoff.
* Rate limits / auth errors / outages trigger an immediate switch to the next provider,
  and rate-limited providers are put on a cooldown so we don't hammer them.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

import httpx

from ..config import Settings, get_settings

log = logging.getLogger("nova.llm")

ErrorKind = Literal["rate_limit", "auth", "unavailable", "timeout", "bad_request", "bad_response", "not_configured"]


class ProviderError(Exception):
    def __init__(self, provider: str, kind: ErrorKind, detail: str = ""):
        super().__init__(f"{provider}: {kind} {detail}".strip())
        self.provider = provider
        self.kind = kind
        self.detail = detail

    @property
    def retryable(self) -> bool:
        return self.kind in ("timeout", "unavailable")


class AllProvidersFailed(Exception):
    def __init__(self, errors: list[ProviderError]):
        self.errors = errors
        summary = "; ".join(f"{e.provider}={e.kind}" for e in errors) or "no providers configured"
        super().__init__(f"All AI providers failed ({summary})")

    @property
    def user_message(self) -> str:
        kinds = {e.kind for e in self.errors}
        if not self.errors or kinds == {"not_configured"}:
            return "No AI provider is configured on the NOVA server (set GEMINI_API_KEY or GROQ_API_KEY)."
        if "rate_limit" in kinds:
            return "The AI providers are rate-limiting requests right now. Please try again in a minute."
        if "auth" in kinds:
            return "The AI provider rejected the server's API key. Check the backend configuration."
        return "The AI providers are temporarily unavailable. Please try again shortly."


@dataclass
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    attempts: list[str] = field(default_factory=list)  # e.g. ["gemini:rate_limit", "groq:ok"]

    def json(self) -> dict:
        return parse_json_loose(self.text)


def parse_json_loose(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start : end + 1])


def _classify_http(provider: str, resp: httpx.Response) -> ProviderError:
    code = resp.status_code
    body = resp.text[:300]
    if code == 429 or "RESOURCE_EXHAUSTED" in body:
        return ProviderError(provider, "rate_limit", f"HTTP {code}")
    if code in (401, 403):
        return ProviderError(provider, "auth", f"HTTP {code}")
    if code >= 500:
        return ProviderError(provider, "unavailable", f"HTTP {code}")
    return ProviderError(provider, "bad_request", f"HTTP {code}: {body}")


class BaseProvider:
    name: str = "base"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.llm_timeout_seconds)

    @property
    def model(self) -> str:
        raise NotImplementedError

    def configured(self) -> bool:
        raise NotImplementedError

    # Per-call timeout, set by the router from the remaining time budget (None → client default).
    call_timeout: float | None = None

    def _post(self, url: str, **kwargs) -> dict:
        if self.call_timeout is not None:
            kwargs.setdefault("timeout", self.call_timeout)
        try:
            resp = self.client.post(url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ProviderError(self.name, "timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, "unavailable", type(exc).__name__) from exc
        if resp.status_code != 200:
            raise _classify_http(self.name, resp)
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(self.name, "bad_response", "invalid JSON body") from exc

    def complete(self, messages: list[ChatMessage], *, json_mode: bool, temperature: float) -> str:
        raise NotImplementedError


class GeminiProvider(BaseProvider):
    name = "gemini"

    def __init__(self, settings: Settings, client: httpx.Client | None = None, clock=time.monotonic):
        super().__init__(settings, client)
        self._clock = clock
        self._degraded: dict[str, float] = {}  # model → skip until (overloaded/timing out)
        self.last_model: str | None = None

    @property
    def models(self) -> list[str]:
        extra = [m.strip() for m in (self.settings.gemini_fallback_models or "").split(",") if m.strip()]
        return list(dict.fromkeys([self.settings.gemini_model, *extra]))

    @property
    def model(self) -> str:
        return self.last_model or self.settings.gemini_model

    def configured(self) -> bool:
        return bool(self.settings.gemini_api_key)

    def complete(self, messages, *, json_mode, temperature):
        """Tries the main model, then the fallback models when one is overloaded (503) or slow.
        An overloaded model is skipped for a minute so later requests don't wait on it again."""
        if not self.configured():
            raise ProviderError(self.name, "not_configured")
        now = self._clock()
        candidates = [m for m in self.models if self._degraded.get(m, 0) <= now] or self.models[-1:]
        budget = self.call_timeout
        last: ProviderError | None = None
        for i, m in enumerate(candidates):
            if budget is not None:  # share the time budget so a hung model can't use all of it
                self.call_timeout = max(1.0, budget / (len(candidates) - i))
            started = self._clock()
            try:
                text = self._complete_model(m, messages, json_mode=json_mode, temperature=temperature)
                self.last_model = m
                return text
            except ProviderError as err:
                last = err
                if err.kind not in ("unavailable", "timeout"):
                    raise
                self._degraded[m] = self._clock() + 60
                log.warning("Gemini model %s %s; trying the next model", m, err.kind)
                if budget is not None:
                    budget -= self._clock() - started
        raise last  # type: ignore[misc]  (the router resets call_timeout afterwards)

    def _complete_model(self, model: str, messages, *, json_mode, temperature):
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        contents = [
            {"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]}
            for m in messages
            if m.role != "system"
        ]
        payload: dict = {
            "contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": 1024},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        data = self._post(
            f"{self.settings.gemini_base_url.rstrip('/')}/models/{model}:generateContent",
            json=payload,
            headers={"x-goog-api-key": self.settings.gemini_api_key or ""},
        )
        try:
            candidate = data["candidates"][0]
            return "".join(p.get("text", "") for p in candidate["content"]["parts"])
        except (KeyError, IndexError, TypeError) as exc:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "empty response")
            raise ProviderError(self.name, "bad_response", str(reason)) from exc


class GroqProvider(BaseProvider):
    name = "groq"

    @property
    def model(self) -> str:
        return self.settings.groq_model

    def configured(self) -> bool:
        return bool(self.settings.groq_api_key)

    def complete(self, messages, *, json_mode, temperature):
        if not self.configured():
            raise ProviderError(self.name, "not_configured")
        payload: dict = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": 1024,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self._post(
            f"{self.settings.groq_base_url.rstrip('/')}/chat/completions", json=payload, headers={"Authorization": f"Bearer {self.settings.groq_api_key}"}
        )
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(self.name, "bad_response", "missing choices") from exc

    def transcribe(self, audio: bytes, filename: str, mime: str) -> str:
        if not self.configured():
            raise ProviderError(self.name, "not_configured")
        try:
            resp = self.client.post(
                f"{self.settings.groq_base_url.rstrip('/')}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.settings.groq_api_key}"},
                data={"model": self.settings.groq_stt_model, "response_format": "json"},
                files={"file": (filename, audio, mime)},
            )
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, "unavailable", type(exc).__name__) from exc
        if resp.status_code != 200:
            raise _classify_http(self.name, resp)
        return (resp.json().get("text") or "").strip()


@dataclass
class ProviderHealth:
    failures: int = 0
    successes: int = 0
    last_error: str | None = None
    cooldown_until: float = 0.0


class LLMRouter:
    """Tries providers in preference order with retries and cooldowns."""

    def __init__(
        self,
        settings: Settings | None = None,
        providers: list[BaseProvider] | None = None,
        sleep=time.sleep,
    ):
        self.settings = settings or get_settings()
        if providers is None:
            client = httpx.Client(timeout=self.settings.llm_timeout_seconds)
            providers = [GeminiProvider(self.settings, client), GroqProvider(self.settings, client)]
        self.providers = {p.name: p for p in providers}
        self.health = {p.name: ProviderHealth() for p in providers}
        self._sleep = sleep

    def order(self, preferred: str | None = None) -> list[BaseProvider]:
        pref = preferred or self.settings.preferred_provider
        names = sorted(self.providers, key=lambda n: 0 if n == pref else 1)
        return [self.providers[n] for n in names]

    def any_configured(self) -> bool:
        return any(p.configured() for p in self.providers.values())

    def status(self) -> dict:
        now = time.monotonic()
        return {
            name: {
                "configured": p.configured(),
                "model": p.model,
                "successes": self.health[name].successes,
                "failures": self.health[name].failures,
                "last_error": self.health[name].last_error,
                "cooling_down": self.health[name].cooldown_until > now,
            }
            for name, p in self.providers.items()
        }

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
        preferred: str | None = None,
        validate=None,
        deadline_s: float | None = None,
    ) -> LLMResult:
        """`validate(text)` may raise ValueError to treat a response as unusable (→ retry/fallback).
        `deadline_s` caps the total time across every retry and fallback; when it runs out the
        caller gets AllProvidersFailed quickly instead of a 40-second wait."""
        errors: list[ProviderError] = []
        attempts: list[str] = []
        end = time.monotonic() + deadline_s if deadline_s else None

        def remaining() -> float | None:
            return None if end is None else end - time.monotonic()

        for provider in self.order(preferred):
            health = self.health[provider.name]
            if not provider.configured():
                errors.append(ProviderError(provider.name, "not_configured"))
                continue
            if health.cooldown_until > time.monotonic():
                attempts.append(f"{provider.name}:cooldown")
                errors.append(ProviderError(provider.name, "rate_limit", "cooling down"))
                continue
            for attempt in range(self.settings.llm_max_retries + 1):
                left = remaining()
                if left is not None and left < 1.0:
                    errors.append(ProviderError(provider.name, "timeout", "time budget used up"))
                    attempts.append(f"{provider.name}:deadline")
                    break
                provider.call_timeout = None if left is None else max(1.0, min(self.settings.llm_timeout_seconds, left))
                try:
                    text = provider.complete(messages, json_mode=json_mode, temperature=temperature)
                    if validate is not None:
                        try:
                            validate(text)
                        except (ValueError, TypeError) as exc:
                            raise ProviderError(provider.name, "bad_response", f"validation: {exc}") from exc
                    health.successes += 1
                    attempts.append(f"{provider.name}:ok")
                    return LLMResult(text=text, provider=provider.name, model=provider.model, attempts=attempts)
                except ProviderError as err:
                    health.failures += 1
                    health.last_error = err.kind
                    attempts.append(f"{provider.name}:{err.kind}")
                    log.warning("LLM provider %s failed (attempt %d): %s", provider.name, attempt + 1, err)
                    if err.kind == "rate_limit":
                        health.cooldown_until = time.monotonic() + self.settings.provider_cooldown_seconds
                    retry = err.retryable or (err.kind == "bad_response" and attempt == 0)
                    if not retry or attempt >= self.settings.llm_max_retries:
                        errors.append(err)
                        break
                    pause = min(0.5 * 2**attempt, 4.0)
                    left = remaining()
                    if left is not None and left - pause < 1.0:
                        errors.append(err)
                        break
                    self._sleep(pause)
                finally:
                    provider.call_timeout = None
            log.info("Falling back from provider %s", provider.name)
        raise AllProvidersFailed(errors)


_router: LLMRouter | None = None


def get_router() -> LLMRouter:
    global _router
    if _router is None:
        _router = LLMRouter()
    return _router


def set_router(router: LLMRouter | None) -> None:
    """Used by tests to inject a router with mocked transports."""
    global _router
    _router = router
