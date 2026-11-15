"""The required acceptance scenarios, exercised end-to-end through the HTTP API.

(Browser-side halves — notification scheduling, chrome.downloads, restart, microphone —
are covered by the Playwright suite in /tests-e2e.)
"""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.llm import providers
from app.models import SavedWebsite
from tests.conftest import TZ

IST = ZoneInfo(TZ)


def ask(client, auth, message, page=None, session="scenario", **kw):
    r = client.post("/api/assistant/message", json={"session_id": session, "message": message, "page": page, "timezone": TZ, **kw}, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def test_1_remind_to_visit_website_tomorrow(client, auth):
    page = {"url": "https://example.com/article", "title": "Example Article"}
    r = ask(client, auth, "Remind me to visit this website tomorrow at 10 AM", page)
    assert r["status"] == "done" and r["intent"] == "create_reminder"
    assert {"type": "sync_reminders", "payload": {}} in r["client_actions"]
    rem = r["data"]["reminder"]
    assert rem["related_url"] == page["url"] and rem["related_title"] == "Example Article"
    local = datetime.fromisoformat(rem["scheduled_at"]).astimezone(IST)
    assert local.hour == 10 and local.date() == datetime.now(IST).date() + timedelta(days=1)
    sites = client.get("/api/websites", headers=auth).json()
    assert sites[0]["url"] == page["url"] and sites[0]["title"] == "Example Article"
    pending = client.get("/api/reminders?status=pending", headers=auth).json()
    assert [p["id"] for p in pending] == [rem["id"]]


def test_2_alarm_for_meeting_at_3pm(client, auth):
    r = ask(client, auth, "Set an alarm for my meeting at 3 PM")
    assert r["status"] == "done" and r["intent"] == "create_alarm"
    rem = r["data"]["reminder"]
    local = datetime.fromisoformat(rem["scheduled_at"]).astimezone(IST)
    now = datetime.now(IST)
    assert local.hour == 15 and local.minute == 0 and local > now
    assert local.date() == (now.date() if now.hour < 15 else now.date() + timedelta(days=1))
    assert rem["kind"] == "alarm" and rem["title"] == "Meeting"
    dashboard = client.get("/api/reminders?status=pending&status=snoozed", headers=auth).json()
    assert dashboard[0]["id"] == rem["id"]


def test_3_save_this_pdf(client, auth):
    page = {"url": "https://arxiv.org/pdf/2401.00001.pdf", "title": "2401.00001.pdf", "content_type": "application/pdf"}
    r = ask(client, auth, "Save this PDF", page)
    assert r["status"] == "done" and r["intent"] == "download_file"
    action = r["client_actions"][0]
    assert action["type"] == "download" and action["payload"]["url"] == page["url"]
    assert action["payload"]["filename"] == "NOVA/2401.00001.pdf"
    files = client.get("/api/files", headers=auth).json()
    assert files[0]["file_name"] == "2401.00001.pdf" and files[0]["status"] == "requested"
    assert files[0]["source_page_url"] == page["url"]


def test_3b_pdf_link_on_html_page_and_sensitive_confirmation(client, auth):
    page = {"url": "https://example.com/papers", "title": "Papers", "file_links": [
        {"url": "https://example.com/files/report.pdf", "text": "Annual report", "ext": "pdf"},
        {"url": "https://example.com/files/setup.exe", "text": "Installer", "ext": "exe"},
    ]}
    r = ask(client, auth, "Download this PDF to my project folder", page, session="pdf")
    assert r["client_actions"][0]["payload"]["filename"] == "NOVA/project/report.pdf"
    r = ask(client, auth, "Download the exe file", {**page, "file_links": [page["file_links"][1]]}, session="exe")
    assert r["status"] == "needs_confirmation" and ".exe" in r["reply"]
    r = ask(client, auth, "yes", page, session="exe")
    assert r["client_actions"][0]["type"] == "download"


def test_4_show_pending_tasks(client, auth):
    for t in ("Write report", "Call bank"):
        client.post("/api/tasks", json={"title": t}, headers=auth)
    done = client.post("/api/tasks", json={"title": "Old thing"}, headers=auth).json()
    client.post(f"/api/tasks/{done['id']}/complete", headers=auth)
    r = ask(client, auth, "Show all my pending tasks")
    assert r["intent"] == "list_tasks"
    assert sorted(t["title"] for t in r["data"]["tasks"]) == ["Call bank", "Write report"]
    assert all(t["status"] == "pending" for t in r["data"]["tasks"])
    assert "2 pending tasks" in r["reply"]


def test_5_gemini_unavailable_groq_handles(client, auth, fake_llm):
    fake_llm.groq = fake_llm.groq_ok(json.dumps({"intent": "list_tasks", "status_filter": "open"}))
    providers.set_router(fake_llm.router())
    r = ask(client, auth, "what's on my plate?")
    assert r["provider"] == "groq" and r["intent"] == "list_tasks" and r["status"] == "done"
    assert {c[0] for c in fake_llm.calls} == {"gemini", "groq"}
    health = client.get("/api/health/providers", headers=auth).json()
    assert health["gemini"]["failures"] >= 1 and health["groq"]["successes"] == 1


def test_5b_both_providers_down_still_works_for_core_actions(client, auth, fake_llm):
    providers.set_router(fake_llm.router())  # both 503
    r = ask(client, auth, "Create a task to renew passport")
    assert r["status"] == "done" and r["provider"] is None
    r = ask(client, auth, "Tell me a joke")
    assert r["status"] == "error" and "temporarily unavailable" in r["reply"]


def test_6_open_website_saved_yesterday(client, auth, db):
    now = datetime.now(timezone.utc)
    client.post("/api/websites", json={"url": "https://old.example.com", "title": "Old"}, headers=auth)
    client.post("/api/websites", json={"url": "https://yesterday.example.com", "title": "Yesterday's find"}, headers=auth)
    client.post("/api/websites", json={"url": "https://today.example.com", "title": "Today"}, headers=auth)
    for url, delta in (("https://old.example.com", 10), ("https://yesterday.example.com", 1)):
        site = db.scalar(select(SavedWebsite).where(SavedWebsite.url == url))
        site.created_at = site.last_visited_at = now - timedelta(days=delta)
    db.commit()
    r = ask(client, auth, "Open the website I saved yesterday")
    assert r["intent"] == "open_website"
    assert r["client_actions"] == [{"type": "open_url", "payload": {"url": "https://yesterday.example.com"}}]


def test_7_pending_reminders_restorable_from_server(client, auth):
    """After a restart the extension rebuilds alarms from GET /reminders (pending + snoozed)."""
    ids = []
    for h in (1, 2):
        ids.append(client.post("/api/reminders", json={"title": f"R{h}", "scheduled_at": (datetime.now(timezone.utc) + timedelta(hours=h)).isoformat()}, headers=auth).json()["id"])
    client.post(f"/api/reminders/{ids[1]}/snooze", json={"minutes": 30}, headers=auth)
    restored = client.get("/api/reminders?status=pending&status=snoozed", headers=auth).json()
    assert {r["id"] for r in restored} == set(ids)
    assert all(r["next_fire_at"] for r in restored)


def test_8_text_interaction_without_microphone(client, auth):
    r = ask(client, auth, "Remind me to stretch in 30 minutes")
    assert r["status"] == "done" and r["intent"] == "create_reminder"
