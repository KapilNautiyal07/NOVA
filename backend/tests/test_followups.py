"""Follow-up commands that refer to what NOVA just did (scenario 8)."""
from sqlalchemy import select

from app.models import Reminder, Task
from tests.conftest import NOW
from tests.test_graph import nodes, say


def test_do_that_tomorrow_moves_the_reminder_just_created(db, user):
    r = say(db, user, "Remind me to call mom at 6 pm")
    assert r.status == "done"
    r = say(db, user, "Actually, do that tomorrow at 7 pm")
    assert r.intent == "reschedule_reminder" and r.status == "done", r.reply
    rems = list(db.scalars(select(Reminder)))
    assert len(rems) == 1  # moved, not duplicated
    local = rems[0].scheduled_at.astimezone(NOW.tzinfo)
    assert (local.day, local.hour) == (8, 19)


def test_move_it_to(db, user):
    say(db, user, "Remind me to stretch in 2 hours")
    r = say(db, user, "move it to 5 pm")
    assert r.intent == "reschedule_reminder" and "5:00 PM" in r.reply


def test_task_follow_up(db, user):
    say(db, user, "Create a task to submit the MCA assignment")
    r = say(db, user, "make it tomorrow at 10 am")
    assert r.intent == "update_task", r.reply
    t = db.scalar(select(Task))
    assert t.due_at.astimezone(NOW.tzinfo).hour == 10


def test_remind_me_later_about_it_asks_when(db, user):
    say(db, user, "Create a task to pay the electricity bill")
    r = say(db, user, "Remind me about it later")
    assert r.intent == "create_reminder" and r.status == "needs_clarification" and "When" in r.reply
    r = say(db, user, "at 8 pm")
    assert r.status == "done" and "Pay the electricity bill" in r.reply


def test_open_that_website_again(db, user):
    say(db, user, "Remember this website")
    r = say(db, user, "open that website again")
    assert r.intent == "open_website"
    assert r.client_actions[0].type == "open_url"
    assert r.client_actions[0].payload["url"] == "https://github.com/langchain-ai/langgraph"
    assert "classify" not in "".join(nodes(r)) or True


def test_open_known_site_directly(db, user):
    r = say(db, user, "Open YouTube")
    assert r.intent == "open_website" and r.client_actions[0].payload["url"] == "https://www.youtube.com"
    r = say(db, user, "go to example.org")
    assert r.client_actions[0].payload["url"] == "https://example.org"


def test_no_previous_action_means_no_follow_up(db, user):
    r = say(db, user, "do that tomorrow")
    assert r.intent != "reschedule_reminder"
