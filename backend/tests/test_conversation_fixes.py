"""Regression tests for multi-turn conversation bugs found by probing the agent graph.

Each test drives the real LangGraph agent (rules-only NLU, fixed clock) through
`handle_message`, exactly like /api/assistant/message does.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.agents.intents import rule_classify
from app.agents.page_agent import extractive_summary
from app.agents.runner import handle_message
from app.config import Settings
from app.llm.providers import LLMRouter
from app.models import Reminder, SavedFile, Task
from app.schemas import AssistantIn
from app.timeparse import parse_when

IST = ZoneInfo("Asia/Kolkata")
MON = datetime(2026, 11, 9, 14, 0, tzinfo=IST)  # Monday 14:00
GH = {"url": "https://github.com/langchain-ai/langgraph", "title": "langchain-ai/langgraph"}
PDF = {"url": "https://example.org/papers/attention.pdf", "title": "attention.pdf", "content_type": "application/pdf"}


@pytest.fixture
def say(db, user):
    router = LLMRouter(Settings(gemini_api_key=None, groq_api_key=None))

    def _say(message, page=None, session="s", now=MON):
        payload = AssistantIn(session_id=session, message=message, page=page, timezone="Asia/Kolkata")
        return handle_message(db, user, payload, router, now=now)

    return _say


def local(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(IST)


# ---------------------------------------------------------------- pending-state handling
def test_new_command_is_not_swallowed_by_pending_question(say, db):
    first = say("Set an alarm for 7")
    assert first.status == "needs_clarification" and first.reply == "Is that 7 AM or 7 PM?"
    out = say("Remind me at 5 PM to stretch")
    assert out.status == "done" and out.intent == "create_reminder"
    rem = out.data["reminder"]
    assert rem["title"] == "Stretch" and local(rem["scheduled_at"]).hour == 17
    assert db.scalar(select(Reminder).where(Reminder.title == "7")) is None


def test_restating_the_same_request_still_answers_the_question(say):
    assert say("Remind me").status == "needs_clarification"
    out = say("remind me tomorrow at 10 am")
    assert out.status == "done" and local(out.data["reminder"]["scheduled_at"]).hour == 10


def test_followup_time_keeps_the_original_page(say, db):
    """Spec C: 'I want to visit this website again tomorrow.' → 'Tomorrow at 10 AM'."""
    first = say("I want to visit this website again tomorrow.", GH)
    assert first.reply.startswith("I've saved this website.") and first.status == "needs_clarification"
    out = say("Tomorrow at 10 AM")  # follow-up sent with no page (user navigated away)
    rem = out.data["reminder"]
    assert rem["related_url"] == GH["url"] and "langchain-ai/langgraph" in rem["title"]
    assert local(rem["scheduled_at"]) == datetime(2026, 11, 10, 10, 0, tzinfo=IST)


def test_pending_state_never_persists_page_text(say, db):
    from app.models import Conversation

    say("I want to read this again later", {**GH, "text": "SECRET PAGE BODY"})
    conv = db.scalar(select(Conversation))
    assert "SECRET PAGE BODY" not in str(conv.state)
    assert conv.state["pending"]["extra"]["page"]["url"] == GH["url"]


def test_defaulted_time_can_be_adjusted(say, db):
    out = say("Remind me to complete my MCA assignment tomorrow")
    assert "I picked 9:00 AM" in out.reply
    moved = say("make it 6 pm")
    assert moved.intent == "reschedule_reminder" and moved.status == "done"
    rem = db.get(Reminder, out.data["reminder"]["id"])
    task = db.get(Task, out.data["task"]["id"])
    assert rem.scheduled_at.astimezone(IST) == datetime(2026, 11, 10, 18, 0, tzinfo=IST)
    assert task.due_at == rem.scheduled_at and task.due_has_time


# ---------------------------------------------------------------- update / reschedule
def test_update_task_due_priority_and_rename(say, db):
    created = say("Create a task to prepare for my interview next Friday")
    tid = created.data["task"]["id"]
    out = say("Reschedule my interview task to Saturday at 5 pm")
    assert out.intent == "update_task" and out.status == "done"
    task = db.get(Task, tid)
    assert task.due_at.astimezone(IST) == datetime(2026, 11, 14, 17, 0, tzinfo=IST) and task.due_has_time
    assert out.data.get("reminder"), "a timed due date should get a reminder"
    assert say("Change the priority of the interview task to urgent").status == "done"
    assert say("Rename the interview task to Prepare for Google interview").status == "done"
    db.refresh(task)
    assert task.priority == "urgent" and task.title == "Prepare for Google interview"
    rem = db.scalar(select(Reminder).where(Reminder.task_id == tid))
    assert rem.title == "Prepare for Google interview", "renaming a task renames its reminder"


def test_rescheduling_a_task_moves_its_reminder(client, auth):
    due = (datetime.now(IST) + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
    task = client.post("/api/tasks", json={"title": "Report", "due_at": due.isoformat(), "due_has_time": True}, headers=auth).json()
    rem = client.post("/api/reminders", json={"title": "Report", "scheduled_at": due.isoformat(), "timezone": "Asia/Kolkata",
                                               "task_id": task["id"]}, headers=auth).json()
    new_due = due + timedelta(days=1, hours=2)
    r = client.patch(f"/api/tasks/{task['id']}", json={"due_at": new_due.isoformat()}, headers=auth)
    assert r.status_code == 200
    moved = client.get(f"/api/reminders/{rem['id']}", headers=auth).json()
    assert local(moved["scheduled_at"]) == new_due


def test_reschedule_reminder_uses_new_time_not_old(say):
    say("Remind me to stretch at 5 PM")
    out = say("Move my 5 PM reminder to 6 PM")
    assert out.intent == "reschedule_reminder" and out.status == "done"
    assert local(out.data["reminder"]["scheduled_at"]).hour == 18


# ---------------------------------------------------------------- classification fixes
@pytest.mark.parametrize(
    "message,kind",
    [("Remind me to call mom in 30 minutes", "reminder"), ("Remind me about the client call tomorrow at 4 pm", "meeting"),
     ("Remind me about my call with Sam at 3pm", "meeting"), ("Remind me about the meeting at 3pm", "meeting")],
)
def test_call_is_only_a_meeting_as_a_noun(message, kind):
    assert rule_classify(message).kind == kind


def test_bare_hour_alarm_and_wake_up(say):
    assert say("Set an alarm for 7").reply == "Is that 7 AM or 7 PM?"
    out = say("Wake me up at 6:30 tomorrow", session="w")
    assert out.status == "done" and local(out.data["reminder"]["scheduled_at"]) == datetime(2026, 11, 10, 6, 30, tzinfo=IST)


def test_meridiem_question_keeps_minutes(say):
    assert say("Set an alarm for 6:30 tomorrow").reply == "Is that 6:30 AM or 6:30 PM?"


@pytest.mark.parametrize(
    "message,intent",
    [("Open the langgraph repo", "open_website"), ("Download the installer", "download_file"),
     ("hello", "greeting"), ("thanks!", "greeting"), ("Rename the report task to Final report", "update_task"),
     ("Make the report task high priority", "update_task"), ("Postpone the dentist reminder to Friday", "reschedule_reminder"),
     ("Find the document I downloaded yesterday", "search_files")],
)
def test_new_rule_intents(message, intent):
    assert rule_classify(message).intent == intent


def test_titles_drop_dangling_connectors(say):
    out = say("Add a high priority task to fix the login bug by Wednesday 5pm")
    assert out.data["task"]["title"] == "Fix the login bug" and out.data["task"]["priority"] == "high"


def test_list_header_does_not_leak_task_priority(say):
    say("Add an urgent task to renew the domain tomorrow")
    say("Add a task to water plants tomorrow")
    out = say("Show all my pending tasks")
    assert out.reply.startswith("You have 2 pending tasks:")


# ---------------------------------------------------------------- time parsing
def test_day_of_month():
    p = parse_when("pay rent on the 1st at 10am", MON)
    assert p.dt == datetime(2026, 12, 1, 10, 0, tzinfo=IST)
    p = parse_when("on the 20th", MON)
    assert p.dt.date().isoformat() == "2026-11-20" and not p.has_time
    p = parse_when("the 31st of next month", MON)
    assert p.dt.date().isoformat() == "2026-12-31"
    assert parse_when("the 1st of march at 9am", MON).dt.date().isoformat() == "2027-03-01"


def test_bare_hour_without_date_picks_next_occurrence():
    assert parse_when("at 8", MON).dt == datetime(2026, 11, 9, 20, 0, tzinfo=IST)  # 2 PM now → 8 PM today
    assert parse_when("at 1", MON).dt == datetime(2026, 11, 10, 1, 0, tzinfo=IST)  # 1 PM passed → 1 AM
    early = MON.replace(hour=6)
    assert parse_when("at 8", early).dt == datetime(2026, 11, 9, 8, 0, tzinfo=IST)


# ---------------------------------------------------------------- files / summaries
def test_saving_the_same_file_twice_reuses_the_record(say, db):
    say("Remember this document", PDF)
    say("Save this PDF", PDF)
    say("Download this file", PDF)
    files = list(db.scalars(select(SavedFile)))
    assert len(files) == 1 and files[0].status == "requested"


def test_download_by_name_picks_matching_link_and_confirms_risky(say):
    page = {"url": "https://example.com/downloads", "title": "Downloads",
            "file_links": [{"url": "https://example.com/setup.exe", "text": "Download installer"},
                           {"url": "https://example.com/guide.pdf", "text": "User guide"}]}
    ask = say("Download the installer", page, session="d")
    assert ask.status == "needs_confirmation" and "setup.exe" in ask.reply
    done = say("yes", session="d")
    assert done.status == "done" and done.client_actions[0].payload["url"] == "https://example.com/setup.exe"


def test_extractive_summary_skips_duplicate_sentences():
    text = "Vector databases store embeddings for similarity search. " * 3 + "HNSW is a graph index that trades memory for recall."
    out = extractive_summary(text)
    assert out.count("Vector databases store embeddings") == 1 and "HNSW" in out


# ---------------------------------------------------------------- overdue semantics
def test_date_only_task_is_not_overdue_on_its_due_day():
    from app.models import Task as T
    from app.services.tasks import is_overdue

    nine_am_today = MON.replace(hour=9)  # date-only tasks are stored at a default hour
    task = T(title="Buy groceries", status="pending", due_at=nine_am_today, due_has_time=False)
    assert not is_overdue(task, MON)  # 2 PM the same day
    assert is_overdue(task, MON + timedelta(days=1))
    timed = T(title="Call", status="pending", due_at=nine_am_today, due_has_time=True)
    assert is_overdue(timed, MON)


def test_list_reply_does_not_flag_todays_date_only_task(say):
    say("Add a task to buy groceries today")
    assert "(overdue)" not in say("Show all my pending tasks").reply
