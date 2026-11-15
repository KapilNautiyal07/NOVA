"""REST API: auth, validation, CRUD, isolation, privacy controls."""
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_endpoints_require_token(client):
    for path in ("/api/reminders", "/api/tasks", "/api/websites", "/api/files", "/api/auth/me"):
        assert client.get(path).status_code == 401
    assert client.get("/api/tasks", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/api/assistant/message", json={"session_id": "x", "message": "hi"}).status_code == 401


def test_register_and_me(client, auth):
    me = client.get("/api/auth/me", headers=auth).json()
    assert me["email"] == "k@example.com" and me["timezone"] == "Asia/Kolkata"
    dup = client.post("/api/auth/register", json={"name": "X", "email": "K@example.com"})
    assert dup.status_code == 409


def test_register_validation(client):
    assert client.post("/api/auth/register", json={"name": "X", "email": "not-an-email"}).status_code == 422
    assert client.post("/api/auth/register", json={"name": "X", "email": "a@b.co", "timezone": "Mars/Base"}).status_code == 422


def test_registration_secret(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "registration_secret", "s3cret")
    assert client.post("/api/auth/register", json={"name": "X", "email": "a@b.co"}).status_code == 403
    r = client.post("/api/auth/register", json={"name": "X", "email": "a@b.co", "registration_secret": "s3cret"})
    assert r.status_code == 201


def test_token_hash_only_stored(client, auth, db):
    from app.models import ApiToken

    raw = auth["Authorization"].split()[1]
    stored = db.query(ApiToken).one()
    assert stored.token_hash != raw and len(stored.token_hash) == 64


def future(hours=2):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def test_reminder_crud_and_actions(client, auth):
    r = client.post("/api/reminders", json={"title": "Stand-up", "scheduled_at": future(), "kind": "meeting", "notify_offsets": [0, 15]}, headers=auth)
    assert r.status_code == 201
    rem = r.json()
    assert rem["notify_offsets"] == [15, 0] and rem["status"] == "pending"
    rid = rem["id"]
    s = client.post(f"/api/reminders/{rid}/snooze", json={"minutes": 10}, headers=auth).json()
    assert s["status"] == "snoozed" and s["next_fire_at"] == s["snoozed_until"]
    u = client.patch(f"/api/reminders/{rid}", json={"title": "Daily stand-up", "scheduled_at": future(3)}, headers=auth).json()
    assert u["title"] == "Daily stand-up" and u["status"] == "pending"
    c = client.post(f"/api/reminders/{rid}/complete", headers=auth).json()
    assert c["status"] == "completed" and c["completed_at"]
    assert client.post(f"/api/reminders/{rid}/snooze", json={"minutes": 5}, headers=auth).status_code == 409
    assert client.delete(f"/api/reminders/{rid}", headers=auth).status_code == 204
    assert client.get("/api/reminders", headers=auth).json() == []


def test_fired_advances_recurring(client, auth):
    rid = client.post("/api/reminders", json={"title": "Water", "scheduled_at": future(-1), "recurrence": "daily"}, headers=auth).json()["id"]
    r = client.post(f"/api/reminders/{rid}/fired", headers=auth).json()
    assert r["status"] == "pending" and datetime.fromisoformat(r["scheduled_at"]) > datetime.now(timezone.utc)
    one = client.post("/api/reminders", json={"title": "Once", "scheduled_at": future(-1)}, headers=auth).json()["id"]
    assert client.post(f"/api/reminders/{one}/fired", headers=auth).json()["status"] == "fired"


@pytest.mark.parametrize("payload", [
    {"title": "x", "scheduled_at": "2026-11-08T10:00:00"},  # naive datetime
    {"title": "", "scheduled_at": "2026-11-08T10:00:00+05:30"},
    {"title": "x", "scheduled_at": "2026-11-08T10:00:00Z", "related_url": "javascript:alert(1)"},
    {"title": "x", "scheduled_at": "2026-11-08T10:00:00Z", "notify_offsets": [-5]},
    {"title": "x", "scheduled_at": "2026-11-08T10:00:00Z", "recurrence": "hourly"},
])
def test_reminder_validation(client, auth, payload):
    assert client.post("/api/reminders", json=payload, headers=auth).status_code == 422


def test_task_crud_filters(client, auth):
    mk = lambda **kw: client.post("/api/tasks", json=kw, headers=auth).json()
    a = mk(title="A", priority="high", due_at=future(1))
    mk(title="B", priority="low")
    assert [t["title"] for t in client.get("/api/tasks?priority=high", headers=auth).json()] == ["A"]
    assert client.patch(f"/api/tasks/{a['id']}", json={"status": "completed"}, headers=auth).json()["completed_at"]
    open_titles = [t["title"] for t in client.get("/api/tasks?status=pending", headers=auth).json()]
    assert open_titles == ["B"]
    assert client.post("/api/tasks", json={"title": "x", "priority": "extreme"}, headers=auth).status_code == 422


def test_websites_and_search(client, auth):
    client.post("/api/websites", json={"url": "https://langchain-ai.github.io/langgraph/", "title": "LangGraph docs"}, headers=auth)
    client.post("/api/websites", json={"url": "https://example.com/cats", "title": "Cats", "notes": "cute"}, headers=auth)
    hits = client.get("/api/websites?q=langgraph", headers=auth).json()
    assert [h["title"] for h in hits] == ["LangGraph docs"] and hits[0]["category"] == "Docs"
    assert client.post("/api/websites", json={"url": "file:///etc/passwd"}, headers=auth).status_code == 422
    v = client.post("/api/websites/visit", json={"url": "https://example.com/cats"}, headers=auth).json()
    assert v["tracked"]
    assert not client.post("/api/websites/visit", json={"url": "https://not-saved.com"}, headers=auth).json()["tracked"]


def test_file_folder_traversal_rejected(client, auth):
    for folder in ("../../Windows", "C:/Users", "a/b/c/d", "bad|name"):
        r = client.post("/api/files", json={"file_url": "https://x.com/a.pdf", "folder": folder}, headers=auth)
        assert r.status_code == 422, folder
    ok = client.post("/api/files", json={"file_url": "https://x.com/a%20b.pdf", "folder": "project/reports"}, headers=auth).json()
    assert ok["file_name"] == "a b.pdf" and ok["status"] == "requested"
    st = client.patch(f"/api/files/{ok['id']}/status", json={"status": "complete", "local_download_id": 7, "size_bytes": 1234}, headers=auth).json()
    assert st["status"] == "complete" and st["local_download_id"] == 7


def test_user_isolation(client, auth):
    rid = client.post("/api/reminders", json={"title": "Mine", "scheduled_at": future()}, headers=auth).json()["id"]
    other = client.post("/api/auth/register", json={"name": "Eve", "email": "eve@example.com"}).json()["token"]
    h2 = {"Authorization": f"Bearer {other}"}
    assert client.get("/api/reminders", headers=h2).json() == []
    assert client.patch(f"/api/reminders/{rid}", json={"title": "pwned"}, headers=h2).status_code == 404
    assert client.delete(f"/api/reminders/{rid}", headers=h2).status_code == 404


def test_assistant_input_validation(client, auth):
    bad = [{"session_id": "../x", "message": "hi"}, {"session_id": "s", "message": ""}, {"session_id": "s", "message": "x" * 2001}]
    for body in bad:
        assert client.post("/api/assistant/message", json=body, headers=auth).status_code == 422


def test_non_http_page_is_ignored(client, auth):
    r = client.post("/api/assistant/message", json={"session_id": "s", "message": "Remember this website", "page": {"url": "javascript:alert(1)", "title": "x"}}, headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "error"


def test_suggest_uses_metadata_only(client, auth):
    r = client.post("/api/assistant/suggest", json={"page": {"url": "https://github.com/foo/bar", "title": "foo/bar"}}, headers=auth).json()
    assert r["page_type"] == "github_repo" and "repository" in r["message"]
    r = client.post("/api/assistant/suggest", json={"page": {"url": "https://boards.greenhouse.io/acme/jobs/123", "title": "Engineer"}}, headers=auth).json()
    assert r["page_type"] == "job_listing"
    r = client.post("/api/assistant/suggest", json={"page": {"url": "https://example.com/paper.pdf"}}, headers=auth).json()
    assert r["page_type"] == "pdf"


def test_export_and_delete_all_data(client, auth):
    client.post("/api/tasks", json={"title": "A"}, headers=auth)
    client.post("/api/assistant/message", json={"session_id": "s", "message": "show my tasks"}, headers=auth)
    exp = client.get("/api/data/export", headers=auth).json()
    assert len(exp["tasks"]) == 1 and exp["conversations"][0]["messages"]
    assert client.delete("/api/data?confirm=nope", headers=auth).status_code == 400
    assert client.delete("/api/data?confirm=DELETE", headers=auth).json()["deleted"]
    exp = client.get("/api/data/export", headers=auth).json()
    assert exp["tasks"] == [] and exp["conversations"] == []
    client.delete("/api/data?confirm=DELETE&delete_account=true", headers=auth)
    assert client.get("/api/auth/me", headers=auth).status_code == 401


def test_rate_limit(client, auth, monkeypatch):
    from app import security

    monkeypatch.setattr(security, "_limiter", security.RateLimiter(3, None))
    codes = [client.post("/api/assistant/message", json={"session_id": "s", "message": "help"}, headers=auth).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_transcribe_without_any_stt_provider_is_503(client, auth):
    r = client.post("/api/voice/transcribe", files={"audio": ("a.webm", b"1234", "audio/webm")}, headers=auth)
    assert r.status_code == 503
    r = client.post("/api/voice/transcribe", files={"audio": ("a.txt", b"1234", "text/plain")}, headers=auth)
    assert r.status_code == 415


def test_cors_allows_extension_origin(client):
    r = client.options("/api/health", headers={"Origin": "chrome-extension://abcdefghijklmnop", "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") == "chrome-extension://abcdefghijklmnop"
    r = client.options("/api/health", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in r.headers
