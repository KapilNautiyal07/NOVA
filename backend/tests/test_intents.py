import pytest

from app.agents.intents import rule_classify


@pytest.mark.parametrize(
    "message, intent",
    [
        ("Remember this website.", "save_website"),
        ("Remind me to visit this website tomorrow.", "create_reminder"),
        ("Open the website I visited yesterday.", "open_website"),
        ("Save this PDF.", "download_file"),
        ("Download this file.", "download_file"),
        ("Set an alarm for my meeting at 3 PM.", "create_alarm"),
        ("Remind me to submit this assignment next Monday.", "create_reminder"),
        ("What is this website about?", "describe_page"),
        ("Summarize this page.", "summarize_page"),
        ("Save this information for later.", "save_note"),
        ("Show all my pending tasks.", "list_tasks"),
        ("Cancel my 5 PM reminder.", "cancel_reminder"),
        ("Remind me about this after two hours.", "create_reminder"),
        ("I want to visit this website again tomorrow.", "create_reminder"),
        ("Show me all the websites I saved.", "search_websites"),
        ("Find that website where I was reading about LangGraph.", "search_websites"),
        ("Open the website I saved last week.", "open_website"),
        ("Remind me to complete my MCA assignment tomorrow.", "create_reminder"),
        ("Create a task to prepare for my interview next Friday.", "create_task"),
        ("Remind me about my project submission after three days.", "create_reminder"),
        ("Show my tasks for today.", "list_tasks"),
        ("Set an alarm for my client meeting tomorrow at 11 AM.", "create_alarm"),
        ("Save this file to my project folder.", "download_file"),
        ("Remember this document.", "remember_file"),
        ("Find the document I downloaded yesterday.", "search_files"),
        ("Snooze it for 15 minutes", "snooze_reminder"),
        ("Mark the interview task as done", "complete_task"),
        ("Delete the grocery task", "delete_task"),
        ("What reminders do I have today?", "list_reminders"),
        ("help", "help"),
        ("What's the capital of France?", "general"),
        ("What is the weather like?", "general"),
    ],
)
def test_rule_intents(message, intent):
    assert rule_classify(message, has_page=True).intent == intent


def test_slots_extracted():
    r = rule_classify("Remind me to watch this video tomorrow at 8 PM", has_page=True)
    assert r.use_current_page and r.title.lower().startswith("watch this video") and not r.todo
    r = rule_classify("Remind me to complete my MCA assignment tomorrow", has_page=True)
    assert r.todo and not r.use_current_page
    r = rule_classify("Download this file to my project folder", has_page=True)
    assert r.folder == "project"
    r = rule_classify("Find that website where I was reading about LangGraph", has_page=True)
    assert r.query == "LangGraph"
    r = rule_classify("Open the website I saved yesterday", has_page=True)
    assert r.period == "yesterday"
    r = rule_classify("Snooze my reminder for 15 minutes", has_page=False)
    assert r.snooze_minutes == 15
    r = rule_classify("Create an urgent task to renew my passport", has_page=False)
    assert r.priority == "urgent" and r.title.startswith("renew my passport")
    r = rule_classify("Remind me about the standup every weekday at 9:30 am, 10 minutes before", has_page=False)
    assert r.recurrence == "weekdays" and r.kind == "meeting" and r.lead_minutes == 10
    r = rule_classify("Cancel my 5 PM reminder", has_page=False)
    assert "5 pm" in r.reference


def test_page_reference_ignored_without_page():
    r = rule_classify("Remember this website", has_page=False)
    assert r.intent == "save_website"
    r = rule_classify("Remind me to watch this video tomorrow at 8 PM", has_page=False)
    assert not r.use_current_page


def test_watch_this_refers_to_current_page():
    r = rule_classify("Remind me to watch this tomorrow at 8 PM", has_page=True)
    assert r.use_current_page and not r.todo
    r = rule_classify("Remind me to finish this task tomorrow", has_page=True)
    assert not r.use_current_page
