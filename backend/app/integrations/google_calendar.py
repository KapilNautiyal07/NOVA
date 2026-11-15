"""Google Calendar via Google's OAuth 2.0 web-server flow.

Security model
* The client secret lives only in the server's environment. The extension never sees it,
  nor any Google access/refresh token: it only receives the consent URL to open.
* Authorization starts only from an explicit user click ("Connect Google Calendar"). Each
  attempt gets a single-use random `state` (stored hashed, 10-minute lifetime) and a PKCE
  verifier, so a forged or replayed callback can't attach someone else's calendar.
* Only the minimum scope is requested: calendar.events (create events and check for clashes).
  If the user unticks it on Google's consent screen, nothing is stored.
* Tokens are encrypted at rest (Fernet) and refreshed server-side; a revoked/expired grant
  deletes the stored tokens and the assistant asks the user to reconnect.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import Integration, OAuthState, User

log = logging.getLogger("nova.calendar")

PROVIDER = "google_calendar"
STATE_TTL = timedelta(minutes=10)


class CalendarError(Exception):
    """Calendar request failed for a reason the user should hear about."""


class CalendarNotConfigured(CalendarError):
    pass


class CalendarAuthError(CalendarError):
    """Not connected, or the grant was revoked/expired → ask the user to (re)connect."""


class CalendarUnavailable(CalendarError):
    """Network problem / Google 5xx / timeout → nothing was changed; safe to retry."""


_client: httpx.Client | None = None


def http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=15.0)
    return _client


def set_http_client(client: httpx.Client | None) -> None:
    """Tests inject an httpx.Client with a MockTransport."""
    global _client
    _client = client


# ------------------------------------------------------------------ config / crypto
def configured(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return bool(s.google_client_id and s.google_client_secret)


def _fernet(settings: Settings) -> Fernet:
    if settings.token_encryption_key:
        return Fernet(settings.token_encryption_key.encode())
    if settings.environment == "production":
        raise CalendarNotConfigured("TOKEN_ENCRYPTION_KEY must be set in production.")
    # Development convenience: derive a stable key from the client secret.
    digest = hashlib.sha256(f"nova-token-key:{settings.google_client_secret}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _enc(settings: Settings, value: str | None) -> str | None:
    return _fernet(settings).encrypt(value.encode()).decode() if value else None


def _dec(settings: Settings, value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet(settings).decrypt(value.encode()).decode()
    except InvalidToken:
        return None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def scopes(settings: Settings) -> list[str]:
    return settings.google_calendar_scopes.split()


# ------------------------------------------------------------------ OAuth
def start_authorization(db: Session, user: User, settings: Settings | None = None) -> dict:
    s = settings or get_settings()
    if not configured(s):
        raise CalendarNotConfigured("Google Calendar isn't set up on this NOVA server (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET).")
    now = datetime.now(timezone.utc)
    db.execute(delete(OAuthState).where(OAuthState.expires_at < now - timedelta(days=1)))
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    row = OAuthState(user_id=user.id, provider=PROVIDER, state_hash=_hash(state),
                     code_verifier_enc=_enc(s, verifier), expires_at=now + STATE_TTL)
    db.add(row)
    db.commit()
    params = {
        "client_id": s.google_client_id,
        "redirect_uri": s.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes(s)),
        "access_type": "offline",  # refresh token, so the user isn't asked again next time
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if user.email:
        params["login_hint"] = user.email
    return {"auth_url": f"{s.google_auth_url}?{urlencode(params)}", "attempt_id": row.id}


@dataclass
class CallbackOutcome:
    outcome: str  # connected | denied | error | invalid
    detail: str = ""


def handle_callback(db: Session, state: str | None, code: str | None, error: str | None,
                    granted_scope: str | None = None, settings: Settings | None = None) -> CallbackOutcome:
    s = settings or get_settings()
    if not state:
        return CallbackOutcome("invalid", "missing state")
    row = db.scalar(select(OAuthState).where(OAuthState.state_hash == _hash(state)))
    now = datetime.now(timezone.utc)
    if not row or row.outcome != "pending" or row.expires_at < now:
        return CallbackOutcome("invalid", "This sign-in link has expired or was already used. Start again from NOVA.")

    def finish(outcome: str, detail: str = "") -> CallbackOutcome:
        row.outcome, row.detail = outcome, detail[:200] or None
        db.commit()
        return CallbackOutcome(outcome, detail)

    if error:
        return finish("denied" if error == "access_denied" else "error", error)
    if not code:
        return finish("error", "missing code")
    verifier = _dec(s, row.code_verifier_enc)
    try:
        resp = http().post(s.google_token_url, data={
            "code": code, "client_id": s.google_client_id, "client_secret": s.google_client_secret,
            "redirect_uri": s.google_redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier,
        })
    except httpx.HTTPError as exc:
        log.warning("token exchange failed: %s", type(exc).__name__)
        return finish("error", "Couldn't reach Google to finish connecting. Please try again.")
    if resp.status_code != 200:
        log.warning("token exchange HTTP %s", resp.status_code)  # body may echo the code; don't log it
        return finish("error", f"Google rejected the sign-in (HTTP {resp.status_code}).")
    tok = resp.json()
    granted = set((tok.get("scope") or granted_scope or "").split())
    if not set(scopes(s)) <= granted:
        # Granular consent: the user unticked calendar access. Don't keep a useless token.
        _revoke(s, tok.get("refresh_token") or tok.get("access_token"))
        return finish("denied", "scope_not_granted")

    integ = db.scalar(select(Integration).where(Integration.user_id == row.user_id, Integration.provider == PROVIDER))
    if not integ:
        integ = Integration(user_id=row.user_id, provider=PROVIDER)
        db.add(integ)
    integ.scopes = " ".join(sorted(granted))
    integ.access_token_enc = _enc(s, tok.get("access_token"))
    if tok.get("refresh_token"):
        integ.refresh_token_enc = _enc(s, tok["refresh_token"])
    integ.expires_at = now + timedelta(seconds=int(tok.get("expires_in") or 3600))
    db.commit()
    try:
        integ.account_label = _primary_calendar_label(db, row.user_id, s)
        db.commit()
    except CalendarError:
        pass  # cosmetic only
    return finish("connected")


def attempt_status(db: Session, user: User, attempt_id: int | None) -> dict | None:
    if not attempt_id:
        return None
    row = db.get(OAuthState, attempt_id)
    if not row or row.user_id != user.id:
        return None
    outcome = row.outcome
    if outcome == "pending" and row.expires_at < datetime.now(timezone.utc):
        outcome = "expired"
    return {"outcome": outcome, "detail": row.detail}


def get_integration(db: Session, user_id: int) -> Integration | None:
    return db.scalar(select(Integration).where(Integration.user_id == user_id, Integration.provider == PROVIDER))


def is_connected(db: Session, user: User) -> bool:
    integ = get_integration(db, user.id)
    return bool(integ and (integ.refresh_token_enc or integ.access_token_enc))


def status(db: Session, user: User, attempt_id: int | None = None) -> dict:
    s = get_settings()
    integ = get_integration(db, user.id)
    return {
        "configured": configured(s),
        "connected": bool(integ and (integ.refresh_token_enc or integ.access_token_enc)),
        "account": integ.account_label if integ else None,
        "scopes": integ.scopes.split() if integ and integ.scopes else [],
        "attempt": attempt_status(db, user, attempt_id),
    }


def _revoke(s: Settings, token: str | None) -> None:
    if not token:
        return
    try:
        http().post(s.google_revoke_url, params={"token": token})
    except httpx.HTTPError:
        pass


def disconnect(db: Session, user: User) -> bool:
    s = get_settings()
    integ = get_integration(db, user.id)
    if not integ:
        return False
    _revoke(s, _dec(s, integ.refresh_token_enc) or _dec(s, integ.access_token_enc))
    db.delete(integ)
    db.commit()
    return True


def _access_token(db: Session, user_id: int, s: Settings) -> str:
    integ = get_integration(db, user_id)
    if not integ:
        raise CalendarAuthError("not connected")
    now = datetime.now(timezone.utc)
    token = _dec(s, integ.access_token_enc)
    if token and integ.expires_at and integ.expires_at > now + timedelta(seconds=60):
        return token
    refresh = _dec(s, integ.refresh_token_enc)
    if not refresh:
        db.delete(integ)
        db.commit()
        raise CalendarAuthError("expired")
    try:
        resp = http().post(s.google_token_url, data={
            "client_id": s.google_client_id, "client_secret": s.google_client_secret,
            "refresh_token": refresh, "grant_type": "refresh_token",
        })
    except httpx.HTTPError as exc:
        raise CalendarUnavailable("network") from exc
    if resp.status_code in (400, 401):  # invalid_grant: revoked by the user or expired
        db.delete(integ)
        db.commit()
        raise CalendarAuthError("revoked")
    if resp.status_code != 200:
        raise CalendarUnavailable(f"HTTP {resp.status_code}")
    tok = resp.json()
    integ.access_token_enc = _enc(s, tok["access_token"])
    integ.expires_at = now + timedelta(seconds=int(tok.get("expires_in") or 3600))
    db.commit()
    return tok["access_token"]


def _request(db: Session, user_id: int, method: str, path: str, *, retry_auth: bool = True, **kw) -> dict:
    s = get_settings()
    token = _access_token(db, user_id, s)
    try:
        resp = http().request(method, f"{s.google_calendar_api}{path}", headers={"Authorization": f"Bearer {token}"}, **kw)
    except httpx.HTTPError as exc:
        raise CalendarUnavailable(type(exc).__name__) from exc
    if resp.status_code == 401 and retry_auth:
        integ = get_integration(db, user_id)
        if integ:
            integ.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)  # force refresh once
            db.commit()
        return _request(db, user_id, method, path, retry_auth=False, **kw)
    if resp.status_code in (401, 403):
        raise CalendarAuthError(f"HTTP {resp.status_code}")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise CalendarUnavailable(f"HTTP {resp.status_code}")
    if resp.status_code >= 400:
        raise CalendarError(f"Google Calendar rejected the request (HTTP {resp.status_code}).")
    return resp.json() if resp.content else {}


def _primary_calendar_label(db: Session, user_id: int, s: Settings) -> str | None:
    data = _request(db, user_id, "GET", "/calendars/primary/events", params={"maxResults": 1})
    return (data.get("summary") or None) if isinstance(data, dict) else None


# ------------------------------------------------------------------ events
def find_conflicts(db: Session, user: User, start: datetime, end: datetime) -> list[dict]:
    data = _request(db, user.id, "GET", "/calendars/primary/events", params={
        "timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
        "orderBy": "startTime", "maxResults": 5,
    })
    return [
        {"summary": e.get("summary") or "(busy)", "start": (e.get("start") or {}).get("dateTime")}
        for e in data.get("items") or []
        if e.get("transparency") != "transparent" and (e.get("start") or {}).get("dateTime")
    ]


def create_event(db: Session, user: User, *, title: str, start: datetime, end: datetime, tz: str,
                 attendees: list[str] | None = None, description: str | None = None,
                 request_id: str | None = None) -> dict:
    body: dict = {
        "summary": title,
        "start": {"dateTime": start.isoformat(), "timeZone": tz},
        "end": {"dateTime": end.isoformat(), "timeZone": tz},
        "description": description or "Scheduled with NOVA.",
    }
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    params = {"sendUpdates": "all" if attendees else "none"}
    if request_id:
        # Idempotency: a retried "yes" after a timeout can't create the event twice.
        body["id"] = request_id
    try:
        return _request(db, user.id, "POST", "/calendars/primary/events", params=params, json=body)
    except CalendarError as exc:
        if request_id and "HTTP 409" in str(exc):  # already created by an earlier attempt
            return _request(db, user.id, "GET", f"/calendars/primary/events/{request_id}")
        raise
