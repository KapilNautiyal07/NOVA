import os

os.environ["DATABASE_URL"] = "sqlite://"  # in-memory, shared via StaticPool
# Tests must never use the developer's real keys/settings from backend/.env: environment
# variables take precedence over the .env file, so blank them explicitly (popping isn't enough).
for _var in ("GEMINI_API_KEY", "GROQ_API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "TOKEN_ENCRYPTION_KEY", "REDIS_URL", "REGISTRATION_SECRET"):
    os.environ[_var] = ""
os.environ["PREFERRED_PROVIDER"] = "gemini"
os.environ["GEMINI_MODEL"] = "gemini-3.8-flash"
os.environ["GROQ_MODEL"] = "openai/gpt-oss-120b"
os.environ["ENVIRONMENT"] = "test"
os.environ["RATE_LIMIT_PER_MINUTE"] = "1000"

import json  # noqa: E402
from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.llm import providers  # noqa: E402
from app.llm.providers import GeminiProvider, GroqProvider, LLMRouter  # noqa: E402
from app.integrations import google_calendar as gcal  # noqa: E402
from app.main import create_app  # noqa: E402
from app.voice import service as voice_service  # noqa: E402
from app.models import User  # noqa: E402

TZ = "Asia/Kolkata"
# Saturday 7 Nov 2026, 14:00 IST
NOW = datetime(2026, 11, 7, 14, 0, tzinfo=ZoneInfo(TZ))


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    providers.set_router(LLMRouter(Settings(gemini_api_key=None, groq_api_key=None)))
    voice_service.set_gemini_voice(None)
    gcal.set_http_client(None)
    yield
    providers.set_router(None)
    voice_service.set_gemini_voice(None)
    gcal.set_http_client(None)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def user(db):
    u = User(name="Kapil", email="kapil@example.com", timezone=TZ, preferences={})
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def client():
    return TestClient(create_app(create_tables=False))


@pytest.fixture
def auth(client):
    r = client.post("/api/auth/register", json={"name": "Kapil", "email": "k@example.com", "timezone": TZ})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


class FakeLLM:
    """Builds real Gemini/Groq provider objects on top of an httpx MockTransport."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.gemini = lambda body: httpx.Response(503, json={"error": "down"})
        self.groq = lambda body: httpx.Response(503, json={"error": "down"})

    @staticmethod
    def gemini_ok(text: str):
        return lambda body: httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})

    @staticmethod
    def groq_ok(text: str):
        return lambda body: httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if "generativelanguage" in request.url.host:
            self.calls.append(("gemini", body))
            return self.gemini(body)
        self.calls.append(("groq", body))
        return self.groq(body)

    def router(self, **settings_kw) -> LLMRouter:
        settings_kw.setdefault("gemini_fallback_models", "")  # one Gemini model unless a test opts in
        s = Settings(gemini_api_key="g-test-key", groq_api_key="q-test-key", llm_max_retries=1, **settings_kw)
        client = httpx.Client(transport=httpx.MockTransport(self.handler))
        return LLMRouter(s, [GeminiProvider(s, client), GroqProvider(s, client)], sleep=lambda _: None)


@pytest.fixture
def fake_llm():
    return FakeLLM()
