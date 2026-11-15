"""AI provider fallback (TEST 5) and resilience."""
import httpx
import pytest

from app.llm.providers import AllProvidersFailed, ChatMessage, GeminiProvider, GroqProvider, LLMRouter
from app.config import Settings

MSG = [ChatMessage("system", "be brief"), ChatMessage("user", "hello")]


def test_gemini_is_primary(fake_llm):
    fake_llm.gemini = fake_llm.gemini_ok("hi from gemini")
    res = fake_llm.router().complete(MSG)
    assert res.provider == "gemini" and res.text == "hi from gemini"
    assert [c[0] for c in fake_llm.calls] == ["gemini"]


def test_falls_back_to_groq_when_gemini_down(fake_llm):
    fake_llm.groq = fake_llm.groq_ok("hi from groq")
    router = fake_llm.router()
    res = router.complete(MSG)
    assert res.provider == "groq" and res.text == "hi from groq"
    # 503 is retryable: gemini tried twice (1 retry), then groq
    assert [c[0] for c in fake_llm.calls] == ["gemini", "gemini", "groq"]
    assert res.attempts == ["gemini:unavailable", "gemini:unavailable", "groq:ok"]
    assert router.status()["gemini"]["failures"] == 2


def test_rate_limit_switches_immediately_and_cools_down(fake_llm):
    fake_llm.gemini = lambda b: httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})
    fake_llm.groq = fake_llm.groq_ok("ok")
    router = fake_llm.router()
    assert router.complete(MSG).provider == "groq"
    assert [c[0] for c in fake_llm.calls] == ["gemini", "groq"]  # no retry on 429
    fake_llm.calls.clear()
    assert router.complete(MSG).provider == "groq"
    assert [c[0] for c in fake_llm.calls] == ["groq"]  # gemini skipped during cooldown
    assert router.status()["gemini"]["cooling_down"]


def test_auth_error_falls_back(fake_llm):
    fake_llm.gemini = lambda b: httpx.Response(403, json={"error": "bad key"})
    fake_llm.groq = fake_llm.groq_ok("ok")
    assert fake_llm.router().complete(MSG).provider == "groq"


def test_timeout_falls_back(fake_llm):
    def boom(_):
        raise httpx.ReadTimeout("slow")

    fake_llm.gemini = boom
    fake_llm.groq = fake_llm.groq_ok("ok")
    assert fake_llm.router().complete(MSG).provider == "groq"


def test_invalid_output_is_retried_then_falls_back(fake_llm):
    fake_llm.gemini = fake_llm.gemini_ok("not json at all")
    fake_llm.groq = fake_llm.groq_ok('{"ok": true}')

    def validate(text):
        import json
        json.loads(text)

    res = fake_llm.router().complete(MSG, json_mode=True, validate=validate)
    assert res.provider == "groq"
    assert fake_llm.calls[-1][1]["response_format"] == {"type": "json_object"}


def test_both_fail_gives_useful_error(fake_llm):
    with pytest.raises(AllProvidersFailed) as exc:
        fake_llm.router().complete(MSG)
    assert "temporarily unavailable" in exc.value.user_message
    assert {e.provider for e in exc.value.errors} == {"gemini", "groq"}


def test_no_keys_configured():
    router = LLMRouter(Settings(gemini_api_key=None, groq_api_key=None))
    assert not router.any_configured()
    with pytest.raises(AllProvidersFailed) as exc:
        router.complete(MSG)
    assert "No AI provider is configured" in exc.value.user_message


def test_preferred_provider_configurable(fake_llm):
    fake_llm.gemini = fake_llm.gemini_ok("g")
    fake_llm.groq = fake_llm.groq_ok("q")
    assert fake_llm.router(preferred_provider="groq").complete(MSG).provider == "groq"


def test_keys_sent_only_in_headers_and_never_reported(fake_llm):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "x"}]}}]})

    router = fake_llm.router()
    router.providers["gemini"].client = httpx.Client(transport=httpx.MockTransport(handler))
    router.complete(MSG)
    assert seen[0].headers["x-goog-api-key"] == "g-test-key"
    assert "g-test-key" not in str(seen[0].url)
    assert "key" not in str(router.status()).replace("api_key", "")  # status never includes secrets


def test_gemini_payload_shape(fake_llm):
    fake_llm.gemini = fake_llm.gemini_ok("{}")
    fake_llm.router().complete(MSG, json_mode=True)
    body = fake_llm.calls[0][1]
    assert body["systemInstruction"]["parts"][0]["text"] == "be brief"
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_overloaded_gemini_model_falls_back_to_backup_model_and_is_skipped_next_time(fake_llm):
    import re as _re
    seen = []

    orig = fake_llm.handler

    def handler(request):
        if "generativelanguage" in request.url.host:
            model = _re.search(r"models/([^:]+):", request.url.path).group(1)
            seen.append(model)
            if model == "gemini-3.8-flash":
                return httpx.Response(503, json={"error": "overloaded"})
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "hi"}]}}]})
        return orig(request)

    fake_llm.handler = handler
    router = fake_llm.router(gemini_fallback_models="gemini-3.5-flash")
    res = router.complete([ChatMessage("user", "hello")])
    assert res.text == "hi" and res.provider == "gemini" and res.model == "gemini-3.5-flash"
    assert seen == ["gemini-3.8-flash", "gemini-3.5-flash"]
    router.complete([ChatMessage("user", "again")])
    assert seen[2:] == ["gemini-3.5-flash"]  # the overloaded model is skipped for a while


def test_deadline_caps_total_time_across_retries_and_fallbacks():
    """A provider that hangs can't make the user wait for every retry × timeout."""
    import time as _time

    calls = []

    def slow(request):
        calls.append(request.url.host)
        raise httpx.ReadTimeout("slow", request=request)

    s = Settings(gemini_api_key="g", groq_api_key="q", llm_max_retries=2, gemini_fallback_models="")
    client = httpx.Client(transport=httpx.MockTransport(slow))
    router = LLMRouter(s, [GeminiProvider(s, client), GroqProvider(s, client)], sleep=lambda _: None)
    started = _time.monotonic()
    with pytest.raises(AllProvidersFailed):
        router.complete([ChatMessage("user", "x")], deadline_s=0.5)
    assert _time.monotonic() - started < 2 and calls == []  # budget < 1 s → nothing worth trying
