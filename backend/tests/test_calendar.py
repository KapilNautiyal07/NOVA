"""Google Calendar: OAuth permission flow + conversational scheduling (fake Google server).

Covers the required scenarios: scheduling without a connection (9), denied permission (10),
successful connection (11), a missing time (12), changing the time before confirming (13),
and Calendar/network failures (14) — plus OAuth hardening (state, PKCE, scopes, refresh).
"""
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.integrations import google_calendar as gcal
from app.models import Integration, OAuthState
from tests.conftest import NOW
from tests.test_graph import say

SCOPE = "https://www.googleapis.com/auth/calendar.events"


class FakeGoogle:
    def __init__(self):
        self.inserted: list[dict] = []
        self.token_requests: list[dict] = []
        self.revoked: list[str] = []
        self.insert_mode = "ok"  # ok | network | 500 | 401
        self.refresh_mode = "ok"  # ok | invalid_grant
        self.granted_scope = SCOPE
        self.busy: list[dict] = []
        self.n = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            form = parse_qs(req.content.decode())
            body = {k: v[0] for k, v in form.items()}
            self.token_requests.append(body)
            if body["grant_type"] == "authorization_code":
                if body.get("code") != "good-code" or not body.get("code_verifier"):
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600,
                                                 "scope": self.granted_scope, "token_type": "Bearer"})
            if self.refresh_mode == "invalid_grant":
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.n += 1
            return httpx.Response(200, json={"access_token": f"at-r{self.n}", "expires_in": 3600})
        if url.startswith("https://oauth2.googleapis.com/revoke"):
            self.revoked.append(req.url.params.get("token"))
            return httpx.Response(200)
        if "/calendars/primary/events" in url:
            assert req.headers["Authorization"].startswith("Bearer at-")
            if req.method == "GET":
                return httpx.Response(200, json={"summary": "kapil@example.com", "items": self.busy})
            if self.insert_mode == "network":
                raise httpx.ConnectError("offline")
            if self.insert_mode == "500":
                return httpx.Response(503, json={"error": "backend"})
            if self.insert_mode == "401":
                return httpx.Response(401, json={"error": "unauth"})
            body = json.loads(req.content)
            self.inserted.append({"params": dict(req.url.params), **body})
            return httpx.Response(200, json={**body, "htmlLink": f"https://calendar.google.com/event?eid={body.get('id', 'x')}"})
        return httpx.Response(404)


@pytest.fixture
def google(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8001/api/integrations/google/callback")
    get_settings.cache_clear()
    fake = FakeGoogle()
    gcal.set_http_client(httpx.Client(transport=httpx.MockTransport(fake.handler)))
    yield fake
    get_settings.cache_clear()


def connect(db, user, fake=None):
    """Run the real OAuth flow end-to-end (consent → callback) for `user`."""
    start = gcal.start_authorization(db, user)
    q = parse_qs(urlparse(start["auth_url"]).query)
    out = gcal.handle_callback(db, q["state"][0], "good-code", None)
    assert out.outcome == "connected", out
    return start


# ------------------------------------------------------------------ OAuth
def test_authorization_url_is_minimal_and_pkce_protected(db, user, google):
    start = gcal.start_authorization(db, user)
    url = urlparse(start["auth_url"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "accounts.google.com"
    assert q["scope"] == SCOPE  # only calendar events — no Gmail, Drive, contacts…
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) >= 43
    assert q["access_type"] == "offline" and q["redirect_uri"].endswith("/api/integrations/google/callback")
    assert "client_secret" not in url.query and "csecret" not in start["auth_url"]
    row = db.scalar(select(OAuthState))
    assert row.state_hash != q["state"]  # only a hash of the state is stored
    assert "csecret" not in row.code_verifier_enc


def test_callback_success_stores_encrypted_tokens_and_is_single_use(db, user, google, client):
    start = gcal.start_authorization(db, user)
    state = parse_qs(urlparse(start["auth_url"]).query)["state"][0]
    r = client.get("/api/integrations/google/callback", params={"state": state, "code": "good-code"})
    assert r.status_code == 200 and "Google Calendar connected" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert google.token_requests[0]["code_verifier"]  # PKCE verifier sent with the exchange
    integ = db.scalar(select(Integration))
    db.refresh(integ)
    assert integ.account_label == "kapil@example.com"
    assert "at-1" not in integ.access_token_enc and "rt-1" not in integ.refresh_token_enc  # encrypted at rest
    # replaying the same callback does nothing
    r = client.get("/api/integrations/google/callback", params={"state": state, "code": "good-code"})
    assert r.status_code == 400 and "expired" in r.text.lower()
    assert len(google.token_requests) == 1


def test_forged_state_is_rejected(db, user, google, client):
    r = client.get("/api/integrations/google/callback", params={"state": "forged", "code": "good-code"})
    assert r.status_code == 400
    assert google.token_requests == [] and db.scalar(select(Integration)) is None


def test_user_denies_permission(db, user, google, client):  # scenario 10
    start = gcal.start_authorization(db, user)
    state = parse_qs(urlparse(start["auth_url"]).query)["state"][0]
    r = client.get("/api/integrations/google/callback", params={"state": state, "error": "access_denied"})
    assert "Not connected" in r.text
    assert gcal.attempt_status(db, user, start["attempt_id"])["outcome"] == "denied"
    assert db.scalar(select(Integration)) is None and google.token_requests == []


def test_unticked_calendar_scope_stores_nothing(db, user, google):
    google.granted_scope = "openid"
    start = gcal.start_authorization(db, user)
    state = parse_qs(urlparse(start["auth_url"]).query)["state"][0]
    out = gcal.handle_callback(db, state, "good-code", None)
    assert out.outcome == "denied" and out.detail == "scope_not_granted"
    assert db.scalar(select(Integration)) is None
    assert google.revoked == ["rt-1"]  # the useless grant is revoked


def test_status_and_disconnect_endpoints(db, user, google, client, auth):
    r = client.get("/api/integrations/google/status", headers=auth)
    assert r.json()["configured"] is True and r.json()["connected"] is False
    r = client.post("/api/integrations/google/connect", headers=auth)
    assert r.status_code == 200 and r.json()["auth_url"].startswith("https://accounts.google.com/")
    attempt = r.json()["attempt_id"]
    state = parse_qs(urlparse(r.json()["auth_url"]).query)["state"][0]
    client.get("/api/integrations/google/callback", params={"state": state, "code": "good-code"})
    st = client.get(f"/api/integrations/google/status?attempt={attempt}", headers=auth).json()
    assert st["connected"] is True and st["attempt"]["outcome"] == "connected"
    assert "token" not in json.dumps(st).lower()  # never expose tokens
    assert client.delete("/api/integrations/google", headers=auth).json() == {"disconnected": True}
    assert google.revoked == ["rt-1"]
    assert client.get("/api/integrations/google/status", headers=auth).json()["connected"] is False


def test_connect_without_server_config_is_503(client, auth):
    r = client.post("/api/integrations/google/connect", headers=auth)
    assert r.status_code == 503 and "GOOGLE_CLIENT_ID" in r.json()["detail"]


def test_expired_access_token_is_refreshed(db, user, google):
    connect(db, user)
    integ = db.scalar(select(Integration))
    integ.expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    db.commit()
    gcal.find_conflicts(db, user, NOW, NOW)
    assert google.token_requests[-1]["grant_type"] == "refresh_token"


def test_revoked_grant_asks_to_reconnect(db, user, google):
    connect(db, user)
    integ = db.scalar(select(Integration))
    integ.expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    db.commit()
    google.refresh_mode = "invalid_grant"
    r = say(db, user, "Schedule a meeting called Budget review tomorrow at 11 am for 1 hour")
    assert r.status == "needs_authorization"  # tokens were deleted → connect again
    assert db.scalar(select(Integration)) is None


# ------------------------------------------------------------------ conversation
def test_schedule_without_connection_asks_permission_first(db, user, google):  # scenario 9
    r = say(db, user, "Schedule my meeting for tomorrow")
    assert r.status == "needs_authorization" and r.intent == "schedule_event"
    assert "permission" in r.reply and "connect your Google Calendar" in r.reply
    assert [a.type for a in r.client_actions] == ["connect_calendar"]
    assert google.token_requests == [] and google.inserted == []  # nothing touched Google


def test_declining_to_connect(db, user, google):  # scenario 10 (in conversation)
    say(db, user, "Schedule my meeting for tomorrow")
    r = say(db, user, "No")
    assert r.status == "done" and "won't connect" in r.reply


def test_full_flow_connect_then_missing_details_then_confirm(db, user, google):  # scenarios 11 + 12
    r = say(db, user, "Schedule my meeting for tomorrow")
    assert r.status == "needs_authorization"
    r = say(db, user, "yes")  # a spoken yes opens Google's consent page (the user still approves there)
    assert r.status == "needs_authorization" and "Opening Google" in r.reply
    assert r.client_actions[0].type == "connect_calendar" and r.client_actions[0].payload["auto"] is True
    connect(db, user)
    r = say(db, user, "I've connected my Google Calendar.")
    assert r.reply.startswith("Your Google Calendar is connected.")
    assert "What time tomorrow, and what's it about?" in r.reply and r.status == "needs_clarification"
    r = say(db, user, "11, project discussion")  # bare 11 → 11 AM; the topic comes in the same answer
    assert r.status == "needs_confirmation"
    assert r.reply.startswith("Just to confirm, should I schedule “Project discussion” tomorrow at 11:00 AM for 30 minutes?")
    r = say(db, user, "make it an hour")
    assert r.reply.startswith("Got it. Just to confirm, should I schedule “Project discussion” tomorrow at 11:00 AM for one hour?")
    assert google.inserted == []  # not before confirmation
    r = say(db, user, "Yes")
    assert r.status == "done" and "is on your Google Calendar" in r.reply
    ev = google.inserted[0]
    assert ev["summary"] == "Project discussion"
    assert ev["start"] == {"dateTime": "2026-11-08T11:00:00+05:30", "timeZone": "Asia/Kolkata"}  # user's timezone
    assert ev["end"]["dateTime"] == "2026-11-08T12:00:00+05:30"
    link = [a for a in r.client_actions if a.type == "show_link"][0]
    assert link.payload["url"].startswith("https://calendar.google.com/")
    assert r.data["event"]["html_link"] == link.payload["url"]


def test_already_connected_does_not_ask_again_but_still_confirms(db, user, google):
    connect(db, user)
    r = say(db, user, "Book a call called Design review with sam@example.com tomorrow at 4 pm for 30 minutes")
    assert r.status == "needs_confirmation" and "connect" not in r.reply.lower()
    assert "“Design review” tomorrow at 4:00 PM for 30 minutes with sam@example.com" in r.reply
    r = say(db, user, "yes please")
    assert r.status == "done" and "invitations to sam@example.com" in r.reply
    assert google.inserted[0]["attendees"] == [{"email": "sam@example.com"}]
    assert google.inserted[0]["params"]["sendUpdates"] == "all"


def test_change_time_before_confirming(db, user, google):  # scenario 13
    connect(db, user)
    r = say(db, user, "Schedule a meeting called Sprint planning tomorrow at 11 am for one hour")
    assert r.status == "needs_confirmation"
    r = say(db, user, "Actually make it 3 pm")
    assert r.status == "needs_confirmation" and "tomorrow at 3:00 PM for one hour" in r.reply
    r = say(db, user, "and make it 30 minutes")
    assert "tomorrow at 3:00 PM for 30 minutes" in r.reply
    r = say(db, user, "call it Sprint kickoff")
    assert "“Sprint kickoff”" in r.reply
    assert google.inserted == []
    r = say(db, user, "yes")
    assert google.inserted[0]["start"]["dateTime"] == "2026-11-08T15:00:00+05:30"
    assert google.inserted[0]["end"]["dateTime"] == "2026-11-08T15:30:00+05:30"
    assert google.inserted[0]["summary"] == "Sprint kickoff"


def test_change_the_time_without_saying_it(db, user, google):
    connect(db, user)
    say(db, user, "Schedule a meeting called Sync tomorrow at 11 am for one hour")
    r = say(db, user, "Change the time")
    assert r.status == "needs_clarification" and "what time" in r.reply.lower()
    r = say(db, user, "4:30 pm")
    assert "tomorrow at 4:30 PM" in r.reply and r.status == "needs_confirmation"


def test_calendar_unreachable_creates_nothing_and_can_retry(db, user, google):  # scenario 14
    connect(db, user)
    say(db, user, "Schedule a meeting called Retro tomorrow at 5 pm for 45 minutes")
    google.insert_mode = "network"
    r = say(db, user, "yes")
    assert r.status == "needs_confirmation" and "was not created" in r.reply
    assert google.inserted == []
    google.insert_mode = "500"
    r = say(db, user, "try again")
    assert "was not created" in r.reply
    google.insert_mode = "ok"
    r = say(db, user, "try again")
    assert r.status == "done" and len(google.inserted) == 1


def test_retry_reuses_event_id_so_it_cannot_double_book(db, user, google):
    connect(db, user)
    say(db, user, "Schedule a meeting called Retro tomorrow at 5 pm for 45 minutes")
    google.insert_mode = "500"
    say(db, user, "yes")
    google.insert_mode = "ok"
    say(db, user, "yes")
    assert len(google.inserted) == 1 and len(google.inserted[0]["id"]) == 32


def test_conflict_is_mentioned_in_the_confirmation(db, user, google):
    connect(db, user)
    google.busy = [{"summary": "Dentist", "start": {"dateTime": "2026-11-08T11:00:00+05:30"}}]
    r = say(db, user, "Schedule a meeting called Planning tomorrow at 11 am for an hour")
    assert "Heads up: you already have “Dentist” at 11:00 AM." in r.reply


def test_new_request_during_dialogue_is_not_swallowed(db, user, google):
    connect(db, user)
    r = say(db, user, "Schedule a meeting tomorrow")
    assert "What time tomorrow" in r.reply
    r = say(db, user, "Show my tasks")
    assert r.intent == "list_tasks"


def test_past_time_is_questioned(db, user, google):
    connect(db, user)
    r = say(db, user, "Schedule a meeting called Standup today at 9 am for 15 minutes")
    assert "already passed" in r.reply and r.status == "needs_clarification"


def test_meeting_reminder_is_still_a_reminder(db, user, google):
    r = say(db, user, "My meeting is at 5 pm, remind me 10 minutes before")
    assert r.intent == "create_reminder"


def test_without_server_config_sets_a_meeting_reminder_instead(db, user):
    r = say(db, user, "Schedule a meeting tomorrow at 3 pm")
    assert "isn't set up" in r.reply and "meeting reminder instead" in r.reply
    assert r.status == "done" and r.intent == "schedule_event"
    assert any(a.type == "sync_reminders" for a in r.client_actions)
    r = say(db, user, "hey nova schedule my meeting for tommorow", session="s2")  # time still missing → asked
    assert r.status == "needs_clarification" and "What time tomorrow" in r.reply
    r = say(db, user, "11 am", session="s2")
    assert r.status == "done" and "tomorrow at 11:00 AM" in r.reply


@pytest.mark.parametrize("text,mins", [
    ("for an hour", 60), ("30 minutes", 30), ("an hour and a half", 90), ("1.5 hours", 90),
    ("half an hour", 30), ("2 hours", 120), ("for 45 mins", 45), ("one hour 15 minutes", 75),
])
def test_parse_duration(text, mins):
    from app.agents.calendar_agent import parse_duration
    assert parse_duration(text) == mins


# ------------------------------------------------------------------ real users, real phrasing
def _dialogue(db, user, lines, session):
    return [say(db, user, line, session=session) for line in lines]


def test_hey_nova_schedule_my_meeting_takes_one_answer(db, user, google):
    """The everyday voice request: one question (time + topic), sensible defaults, confirm, done."""
    connect(db, user)
    a, b, c = _dialogue(db, user, ["hey nova schedule my meeting for tommorow", "11 am", "haan"], "v1")
    assert a.status == "needs_clarification" and a.reply == "Sure. What time tomorrow, and what's it about?"
    assert b.status == "needs_confirmation"
    assert b.reply == "Just to confirm, should I schedule “Meeting” tomorrow at 11:00 AM for 30 minutes?"
    assert "Rename it" in b.suggestions and "Make it 1 hour" in b.suggestions
    assert c.status == "done"
    ev = google.inserted[0]
    assert (ev["summary"], ev["start"]["dateTime"], ev["end"]["dateTime"]) == (
        "Meeting", "2026-11-08T11:00:00+05:30", "2026-11-08T11:30:00+05:30")


@pytest.mark.parametrize("answer,title,start", [
    ("11 am project discussion", "Project discussion", "T11:00"),
    ("at 3, it's about the budget", "Budget", "T15:00"),
    ("morning", "Meeting", "T09:00"),
    ("11 am please", "Meeting", "T11:00"),
    ("4 pm then, sprint planning please", "Sprint planning", "T16:00"),
])
def test_time_answer_can_carry_the_topic(db, user, google, answer, title, start):
    connect(db, user)
    _, r = _dialogue(db, user, ["Schedule my meeting for tomorrow", answer], "t")
    assert r.status == "needs_confirmation"
    assert r.data["event_draft"]["title"] == title and start in r.data["event_draft"]["start"]


def test_topic_first_then_time(db, user, google):
    connect(db, user)
    _, a, b = _dialogue(db, user, ["Schedule my meeting for tomorrow", "project discussion", "at 4"], "tf")
    assert a.reply == "What time tomorrow should I schedule “Project discussion”?"
    assert b.reply.startswith("Just to confirm, should I schedule “Project discussion” tomorrow at 4:00 PM")


@pytest.mark.parametrize("yes", ["haan", "ha kar do", "theek hai", "ji", "sounds good", "ok", "perfect"])
def test_indian_english_and_hinglish_confirmations(db, user, google, yes):
    connect(db, user)
    _, r = _dialogue(db, user, ["schedule a meeting tomorrow at 3", yes], "y")
    assert r.status == "done" and google.inserted[0]["summary"] == "Meeting"


@pytest.mark.parametrize("no", ["nahi", "rehne do", "no thanks", "cancel"])
def test_hinglish_no_cancels_without_creating(db, user, google, no):
    connect(db, user)
    _, r = _dialogue(db, user, ["schedule a meeting tomorrow at 3", no], "n")
    assert "won't schedule" in r.reply and google.inserted == []


def test_hinglish_time_words(db, user, google):
    connect(db, user)
    r = say(db, user, "schedule meeting tomorrow 11 baje", session="b")
    assert r.data["event_draft"]["start"].startswith("2026-11-08T11:00")
    r = say(db, user, "schedule a meeting kal shaam 6 baje", session="b2")  # "kal" is ambiguous → still asks the day
    assert r.status in ("needs_clarification", "needs_confirmation")


def test_name_without_email_is_mentioned_in_the_confirmation_not_a_separate_question(db, user, google):
    connect(db, user)
    a, b, _ = _dialogue(db, user, ["Schedule my meeting for tomorrow at 11 with Rahul", "rahul@example.com", "yes"], "r")
    assert a.status == "needs_confirmation" and "“Meeting with Rahul”" in a.reply and "To send Rahul an invite" in a.reply
    assert "with rahul@example.com" in b.reply and "To send" not in b.reply
    assert google.inserted[0]["attendees"] == [{"email": "rahul@example.com"}]


def test_naming_a_default_titled_meeting_at_the_confirmation(db, user, google):
    connect(db, user)
    _, r = _dialogue(db, user, ["schedule meeting tomorrow at 11", "Client call"], "n1")
    assert r.reply.startswith("Got it. Just to confirm, should I schedule “Client call”")
    _, a, b = _dialogue(db, user, ["schedule a meeting tomorrow evening", "Rename it", "Budget review"], "n2")
    assert a.reply == "Sure — what should I call it?"
    assert "“Budget review” tomorrow at 7:00 PM" in b.reply


def test_yes_to_a_title_question_never_becomes_the_title(db, user, google):
    connect(db, user)
    _, _, r = _dialogue(db, user, ["schedule a meeting tomorrow at 3", "Rename it", "ok"], "yt")
    assert "“Meeting”" in r.reply and "“Ok”" not in r.reply


def test_new_request_during_confirmation_is_not_taken_as_a_title(db, user, google):
    connect(db, user)
    _, r = _dialogue(db, user, ["schedule my meeting tomorrow at 11", "open youtube"], "ny")
    assert r.intent == "open_website" and google.inserted == []


def test_claiming_connected_before_google_finished(db, user, google):
    _, _, r = _dialogue(db, user, ["Schedule my meeting for tomorrow", "yes", "done"], "cl")
    assert r.status == "needs_authorization" and "don't see the connection yet" in r.reply
    assert not r.client_actions[0].payload.get("auto")  # not reopened without being asked


def test_details_given_while_not_connected_are_kept(db, user, google):
    _dialogue(db, user, ["Schedule my meeting for tomorrow", "at 11 am"], "k")
    connect(db, user)
    r = say(db, user, "I've connected my Google Calendar.", session="k")
    assert r.status == "needs_confirmation" and "tomorrow at 11:00 AM" in r.reply


def test_default_length_is_configurable(db, user, google, monkeypatch):
    monkeypatch.setenv("CALENDAR_DEFAULT_MINUTES", "60")
    get_settings.cache_clear()
    connect(db, user)
    r = say(db, user, "Schedule a meeting tomorrow at 10 am", session="len")
    assert "for one hour" in r.reply and "Make it 1 hour" not in r.suggestions
