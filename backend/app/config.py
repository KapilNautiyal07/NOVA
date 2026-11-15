"""Application configuration, loaded from environment variables / .env."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "NOVA"
    environment: Literal["development", "production", "test"] = "development"

    # Database: PostgreSQL in production; SQLite is supported for local dev/tests.
    database_url: str = "sqlite:///./nova.db"
    # Optional Redis (rate limiting). Falls back to in-process limiter when unset/unreachable.
    redis_url: str | None = None

    # AI providers — keys live ONLY on the server.
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.8-flash"
    # Tried (comma-separated, in order) when the main Gemini model is overloaded or timing out.
    gemini_fallback_models: str = "gemini-3.5-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_stt_model: str = "whisper-large-v3-turbo"
    preferred_provider: Literal["gemini", "groq"] = "gemini"

    # Voice. Speech-to-text tries these providers in order (Gemini's dedicated transcription
    # model first, Groq Whisper as the fallback). Spoken replies use Gemini TTS; the extension
    # falls back to the browser's voice when the server can't synthesize.
    stt_providers: str = "gemini,groq"
    gemini_stt_model: str = "gemini-3.5-transcribe"
    stt_language_codes: str = "en-IN,en-US"  # BCP-47 hints; empty = auto-detect
    stt_vocabulary: str = "NOVA,Google Calendar,Google Meet,YouTube,GitHub,LinkedIn,WhatsApp,Gmail"
    gemini_tts_model: str = "gemini-3.8-flash-tts"
    # Used when the main TTS model is rate-limited (free tier: a few requests/minute).
    gemini_tts_fallback_model: str = "gemini-3.8-flash-lite-tts"
    tts_default_voice: str = "Kore"
    tts_style: str = "Warm, clear and natural, like a friendly personal assistant talking to someone they know. Relaxed pace with natural pauses."
    voice_timeout_seconds: float = 30.0

    # Google Calendar (OAuth 2.0 web-server flow; secrets stay on this server).
    google_client_id: str | None = None
    google_client_secret: str | None = None
    # Must exactly match an "Authorized redirect URI" of the OAuth client in Google Cloud.
    google_redirect_uri: str = "http://localhost:8000/api/integrations/google/callback"
    google_calendar_scopes: str = "https://www.googleapis.com/auth/calendar.events"
    google_auth_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    google_token_url: str = "https://oauth2.googleapis.com/token"
    google_revoke_url: str = "https://oauth2.googleapis.com/revoke"
    google_calendar_api: str = "https://www.googleapis.com/calendar/v3"
    # Length used when the user doesn't say ("schedule my meeting tomorrow at 11"). It is shown
    # in the confirmation, so the user can still change it — NOVA doesn't ask a separate question.
    calendar_default_minutes: int = 30
    # Fernet key used to encrypt stored OAuth tokens. Generate one with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Required in production; in development a key is derived from GOOGLE_CLIENT_SECRET.
    token_encryption_key: str | None = None
    llm_timeout_seconds: float = 20.0
    # Upper bound on understanding one command (all retries/fallbacks included). After this NOVA
    # uses its deterministic rule parser instead of making the user wait.
    intent_deadline_seconds: float = 8.0
    chat_deadline_seconds: float = 25.0
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    provider_cooldown_seconds: int = 60  # skip a provider for this long after a rate-limit

    # Security
    # When set, /api/auth/register requires this secret (recommended for any non-local deployment).
    registration_secret: str | None = None
    allowed_origins: str = "chrome-extension://*,http://localhost,http://127.0.0.1"
    rate_limit_per_minute: int = 60

    # Privacy
    max_page_chars: int = 6000  # hard cap on page text the server will forward to an AI provider
    max_audio_bytes: int = 10 * 1024 * 1024

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
