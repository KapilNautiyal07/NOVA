"""Intent schema + classification (LLM first, deterministic rules as fallback)."""
from __future__ import annotations

import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..llm.providers import AllProvidersFailed, ChatMessage, LLMRouter, parse_json_loose
from ..timeparse import parse_recurrence

log = logging.getLogger("nova.intent")

Intent = Literal[
    "create_reminder", "create_alarm", "list_reminders", "cancel_reminder", "snooze_reminder",
    "reschedule_reminder",
    "create_task", "list_tasks", "complete_task", "delete_task", "update_task",
    "save_website", "save_note", "search_websites", "open_website",
    "download_file", "remember_file", "search_files",
    "summarize_page", "describe_page",
    "schedule_event",
    "help", "greeting", "general",
]

INTENT_AGENT = {
    "create_reminder": "reminder", "create_alarm": "reminder", "list_reminders": "reminder",
    "cancel_reminder": "reminder", "snooze_reminder": "reminder", "reschedule_reminder": "reminder",
    "create_task": "task", "list_tasks": "task", "complete_task": "task", "delete_task": "task",
    "update_task": "task",
    "save_website": "memory", "save_note": "memory", "search_websites": "memory", "open_website": "memory",
    "download_file": "file", "remember_file": "file", "search_files": "file",
    "summarize_page": "page", "describe_page": "page",
    "schedule_event": "calendar",
    "help": "chat", "greeting": "chat", "general": "chat",
}


class IntentResult(BaseModel):
    intent: Intent
    title: str | None = Field(default=None, max_length=300)
    when: str | None = Field(default=None, max_length=200)  # verbatim time expression
    datetime_iso: str | None = Field(default=None, max_length=40)
    query: str | None = Field(default=None, max_length=300)
    period: str | None = Field(default=None, max_length=100)  # past period for searches
    priority: Literal["low", "medium", "high", "urgent"] | None = None
    recurrence: Literal["daily", "weekdays", "weekly", "monthly"] | None = None
    kind: Literal["reminder", "alarm", "meeting"] | None = None
    notes: str | None = Field(default=None, max_length=2000)
    folder: str | None = Field(default=None, max_length=120)
    file_type: str | None = Field(default=None, max_length=20)
    reference: str | None = Field(default=None, max_length=300)  # which reminder/task
    lead_minutes: int | None = Field(default=None, ge=0, le=1440)
    snooze_minutes: int | None = Field(default=None, ge=1, le=10080)
    due_filter: Literal["today", "tomorrow", "upcoming", "overdue", "all"] | None = None
    status_filter: Literal["open", "completed", "all"] | None = None
    use_current_page: bool = False
    todo: bool = False  # reminder is about doing something → also track as a task
    source: Literal["llm", "rules"] = "rules"

    @field_validator("*", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        return None if v in ("", "null", "none", "None") else v

    @field_validator("use_current_page", "todo", mode="before")
    @classmethod
    def _null_flag_is_false(cls, v):
        # Some models (e.g. gpt-oss on Groq) emit null for unset boolean flags.
        return False if v is None else v


# --------------------------------------------------------------------------- rules
_PAGE_REF = r"\b(this|the current|current|that)\s+(website|site|page|video|article|link|url|repo|repository|post|job|listing|doc|documentation|tab|thread|course|tutorial|product)\b"
_THIS_ONLY = r"\babout\s+this\b|\bthis\s*$"
CONSUME_VERBS = ("visit", "watch", "read", "check", "look", "open", "revisit", "see", "view", "go back", "finish watching", "finish reading", "continue")
_FOLDER_RE = re.compile(r"\b(?:to|in|into|under)\s+(?:my|the)?\s*([a-z0-9][a-z0-9 _\-]{0,30}?)\s+folder\b")
_PRIORITY_RE = re.compile(r"\b(urgent|high|low|medium)(?:\s*-?\s*priority)?\b|\b(important|asap|critical)\b")
_LEAD_RE = re.compile(r"\b(\d+|ten|fifteen|five|thirty)\s*(min(?:ute)?s?|hours?)\s+(?:before|early|ahead)\b")
_WORDNUM = {"five": 5, "ten": 10, "fifteen": 15, "thirty": 30, "twenty": 20}


def _extract_after(text: str, patterns: list[str]) -> str | None:
    for p in patterns:
        m = re.search(p, text, flags=re.I)
        if m:
            return text[m.end():].strip(" .,!?")
    return None


def _priority(low: str) -> str | None:
    m = _PRIORITY_RE.search(low)
    if not m:
        return None
    word = m.group(1) or m.group(2)
    return {"important": "high", "asap": "urgent", "critical": "urgent"}.get(word, word)


def _snooze_minutes(low: str) -> int | None:
    m = re.search(r"\b(\d+|ten|five|fifteen|thirty|twenty)\s*(min(?:ute)?s?|hours?|hrs?)\b", low)
    if not m:
        return None
    n = int(m.group(1)) if m.group(1).isdigit() else _WORDNUM[m.group(1)]
    return n * 60 if m.group(2).startswith("h") else n


def _period(low: str) -> str | None:
    m = re.search(
        r"\b(yesterday|today|last week|this week|last month|recently|lately|the other day|\d+\s+days?\s+ago|"
        r"(?:a|one|two|three)\s+(?:days?|weeks?)\s+ago|(?:on|last)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
        low,
    )
    return m.group(1) if m else None


def _topic(text: str) -> str | None:
    m = re.search(r"\b(?:about|on|regarding|related to|for|called|named|titled|with)\s+(.+)$", text, flags=re.I)
    if m:
        topic = re.sub(r"\b(yesterday|today|last week|this week|last month|recently|that i saved|i saved)\b.*$", "", m.group(1), flags=re.I)
        return topic.strip(" .?!") or None
    return None


def rule_classify(message: str, has_page: bool = False) -> IntentResult:
    text = message.strip()
    low = text.lower().replace("’", "'")
    low = re.sub(r"^(hey|hi|ok|okay)?\s*nova[,!.]?\s*", "", low)
    page_ref = bool(re.search(_PAGE_REF, low)) or bool(re.search(_THIS_ONLY, low)) or bool(
        re.search(r"\b(?:watch|read|visit|revisit|check|see|view|finish|open|review|look at)\s+(?:this|it)\b(?!\s+(?:task|reminder|alarm))", low)
    )
    recurrence = parse_recurrence(low)
    priority = _priority(low)

    def R(intent: str, **kw) -> IntentResult:
        kw.setdefault("use_current_page", page_ref and has_page)
        kw.setdefault("recurrence", recurrence)
        return IntentResult(intent=intent, **kw)

    if re.fullmatch(r"(help|what can you do\??|how do (i|you) use (this|you)\??|commands)", low):
        return R("help")
    if re.fullmatch(r"(hi|hello|hey|hiya|yo|good (morning|afternoon|evening)|thanks|thank you|thx|cheers|great|awesome|cool|nice)( there| nova| so much| a lot)?[!. ]*", low):
        return R("greeting", use_current_page=False)

    # ---- edits: reschedule / reprioritise / rename (before create rules: "move X to Friday")
    m = re.match(
        r"^(?:please\s+)?(?:reschedule|postpone|move|push(?:\s+back)?|shift|change|delay)\s+(?:my\s+|the\s+)?(.+?)\s+(?:to|until|till|for)\s+(.+?)[.!?]*$",
        low,
    )
    if m and not re.search(r"\bpriority\b", low):
        ref, when = m.group(1).strip(), m.group(2).strip()
        if re.search(r"\b(reminders?|alarms?|meetings?)\b", ref):
            ref = _strip_words(ref, r"\b(reminders?|alarms?|meetings?|the|my|for|about)\b")
            return R("reschedule_reminder", reference=ref or None, when=when, use_current_page=False)
        ref = _strip_words(ref, r"\b(tasks?|to-?dos?|the|my|due date( of| for)?)\b")
        return R("update_task", reference=ref or None, when=when, use_current_page=False)
    m = re.search(r"\b(?:change|set|make|update|bump|raise|lower)\s+(?:the\s+)?priority\s+(?:of|for|on)\s+(.+?)\s+to\s+(low|medium|high|urgent)\b", low) \
        or re.search(r"\bmake\s+(.+?)\s+(?:an?\s+)?(low|medium|high|urgent)(?:\s*-?\s*priority)?\s*[.!]*$", low)
    if m and not re.search(r"\b(reminders?|alarms?)\b", m.group(1)):
        ref = _strip_words(m.group(1), r"\b(tasks?|the|my)\b")
        return R("update_task", reference=ref or None, priority=m.group(2), use_current_page=False)
    m = re.search(r"\brename\s+(?:the\s+|my\s+)?(?:task\s+)?(.+?)\s+(?:task\s+)?to\s+(.+)$", text, flags=re.I)
    if m:
        ref = _strip_words(m.group(1).lower(), r"\b(tasks?|the|my)\b")
        return R("update_task", reference=ref or None, title=m.group(2).strip(" .\"'“”"), use_current_page=False)

    asks = r"^(?:please\s+)?(?:can you\s+|could you\s+)?(?:show|list|what|what are|what's|whats|what do i have|display|view|see|get|give me|tell me|do i have|any|read out|which)\b"

    # ---- listing reminders / alarms
    if re.search(asks, low) and re.search(r"\b(reminders?|alarms?|meetings?|schedule|agenda)\b", low) and not re.search(r"\b(remind me|set)\b", low):
        due = "today" if "today" in low else "tomorrow" if "tomorrow" in low else None
        status = "completed" if re.search(r"\b(completed|done|past|old)\b", low) else None
        return R("list_reminders", due_filter=due, status_filter=status, use_current_page=False)

    # ---- listing tasks
    if (re.search(asks, low) and re.search(r"\b(tasks?|to-?dos?|to do list)\b", low)) or re.fullmatch(
        r"(my |all my |pending |open )*(tasks|to-?dos)\??", low
    ):
        due = next((d for d in ("today", "tomorrow", "overdue", "upcoming") if d in low), None)
        if "this week" in low or "next" in low:
            due = "upcoming"
        status = "completed" if re.search(r"\b(completed|done|finished)\b", low) else "all" if re.search(r"\ball tasks\b.*\bincluding\b", low) else "open"
        return R("list_tasks", due_filter=due, status_filter=status, priority=priority, use_current_page=False)

    # ---- file search (before downloads: "the document I downloaded yesterday")
    if re.search(r"\b(find|show|where(?:'s| is| are)?|search|list|locate|which|open)\b", low) and re.search(
        r"\b(documents?|files?|pdfs?|downloads?|attachments?|papers?)\b", low
    ) and not re.search(r"\b(download|save)\s+(this|the|that)\b", low):
        return R("search_files", query=_topic(text) or _strip_words(low, r"\b(find|show|me|where|search|list|locate|which|open|my|the|all|that|i|downloaded|saved)\b"),
                 period=_period(low), use_current_page=False)

    # ---- cancel / delete reminders
    if re.search(r"\b(cancel|delete|remove|turn off|disable|clear|stop|dismiss|drop)\b", low) and re.search(r"\b(reminders?|alarms?|meetings?)\b", low):
        ref = _strip_words(low, r"\b(please|cancel|delete|remove|turn off|disable|clear|stop|dismiss|drop|my|the|reminders?|alarms?|for|about|that|set|i)\b")
        return R("cancel_reminder", reference=ref or None, use_current_page=False)

    if re.search(r"\bsnooze\b", low):
        ref = _strip_words(low, r"\b(snooze|my|the|reminder|alarm|for|by|it|\d+|minutes?|mins?|hours?|ten|five|fifteen|thirty|twenty)\b")
        return R("snooze_reminder", snooze_minutes=_snooze_minutes(low) or 10, reference=ref or None, use_current_page=False)

    # ---- complete / delete tasks
    if not re.search(r"\bremind\b", low):
        m = re.search(r"\b(?:mark|check off|tick off)\s+(.+?)\s+(?:as\s+)?(?:done|complete|completed|finished)\b", low)
        if m or re.search(r"^(?:i\s+)?(?:have\s+)?(?:completed|finished|done with|complete|finish)\b", low):
            ref = m.group(1) if m else re.sub(r"^(?:i\s+)?(?:have\s+)?(?:completed|finished|done with|complete|finish)\s+", "", low)
            ref = re.sub(r"\b(the|my|task)\b", " ", ref).strip(" .")
            return R("complete_task", reference=ref, use_current_page=False)
        if re.search(r"\b(delete|remove|drop|get rid of)\b", low) and re.search(r"\b(tasks?|to-?dos?)\b", low):
            ref = _strip_words(low, r"\b(please|delete|remove|drop|get rid of|the|my|tasks?|to-?dos?|from|list|called|named)\b")
            return R("delete_task", reference=ref or None, use_current_page=False)

    # ---- alarms
    if re.search(r"\b(alarm|wake me)\b", low):
        title = _extract_after(text, [r"\balarm\s+(?:for|to|about)\s+", r"\bwake me up\s+(?:for|to)?\s*"])
        when = text
        # "Set an alarm for 7" / "for 6:30 am tomorrow" — the "title" is really the time.
        bare = re.fullmatch(
            r"(\d{1,2}(?:[:.]\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)((?:\s+(?:on|this|next|today|tonight|tomorrow|tmrw|in the|"
            r"morning|afternoon|evening|night|monday|tuesday|wednesday|thursday|friday|saturday|sunday))*)",
            (title or "").strip(" .!?").lower(),
        )
        if bare:
            title, when = None, f"at {bare.group(1)}{bare.group(2)}"
        return R("create_alarm", title=title, when=when, kind="alarm", lead_minutes=_lead(low), use_current_page=False)

    # ---- Google Calendar events ("schedule a meeting", "add lunch with Sam to my calendar")
    if not re.search(r"\b(remind|alarm|task|to-?do)\b", low) and (
        re.search(r"\b(schedule|book|set up|arrange|organi[sz]e|plan|create|fix)\b.*\b(meeting|event|call|appointment|interview|sync|standup|stand-up|session|catch-?up|demo|review)\b", low)
        or re.search(r"\b(add|put|schedule|create|block)\b.*\b(?:on|to|in|into)\s+(?:my\s+)?(?:google\s+)?calendar\b", low)
    ):
        title = _extract_after(text, [r"\b(?:schedule|book|set up|arrange|organi[sz]e|plan|create|fix|add|put|block)\s+(?:a|an|the|my|our)?\s*"])
        return R("schedule_event", title=title, when=text, kind="meeting", use_current_page=False)

    # ---- explicit tasks
    if re.search(r"\b(create|add|make|new|put|set up|log)\b.*\b(task|to-?do|to do list|todo list)\b", low):
        title = _extract_after(text, [r"\btasks?\s*(?:to|for|called|named|:)\s*", r"\b(?:add|put)\s+"])
        if title:
            title = re.sub(r"\s+(?:to|on|in)\s+my\s+(?:task|to-?do|to do)\s*list\b.*", "", title, flags=re.I)
        return R("create_task", title=title, when=text, priority=priority)

    # ---- meeting reminders
    if re.search(r"\bremind\b", low) or re.search(r"\b(don't let me forget|do not let me forget|ping me)\b", low):
        title = _extract_after(text, [r"\bremind me\s+(?:to|about|of|that)\s+", r"\bremind me\s+", r"\bforget\s+(?:to|about)?\s*", r"\bping me\s+(?:to|about)?\s*"])
        # "call" is only a meeting as a noun ("the client call", "call with Sam"), not "call mom".
        meeting = re.search(r"\b(meeting|interview|standup|stand-up|appointment|sync|webinar)\b", low) or re.search(
            r"\b(?:a|the|my|our|client|team|sales|zoom|video|phone|conference|scheduled)\s+call\b|\bcall\s+with\b", low
        )
        kind = "meeting" if meeting else "reminder"
        verb_match = re.match(r"\s*(\w+(?:\s+\w+)?)", (title or "").lower())
        first = verb_match.group(1) if verb_match else ""
        todo = bool(title) and not first.startswith(CONSUME_VERBS) and not re.match(r"\s*(this|it|that)\b", (title or "").lower()) and kind == "reminder"
        return R("create_reminder", title=title, when=text, kind=kind, lead_minutes=_lead(low), todo=todo, priority=priority)

    # ---- notes
    if re.search(r"\b(save|remember|note|keep|store|jot)\b", low) and re.search(
        r"\b(this|that|the)\s+(information|info|text|quote|snippet|selection|passage|paragraph|fact|detail)s?\b|\bfor later\b|\btake a note\b|\bnote that\b",
        low,
    ):
        note = _extract_after(text, [r"\bnote that\s+", r"\btake a note\s*:?\s*", r"\bremember that\s+"])
        return R("save_note", notes=note, use_current_page=has_page)

    # ---- files
    file_word = r"\b(pdfs?|files?|documents?|docs?|images?|attachments?|papers?|slides|ebooks?|spreadsheets?|zip|presentation)\b"
    if re.search(r"\b(remember|note)\b", low) and re.search(file_word, low):
        return R("remember_file", file_type=_file_type(low), notes=None)
    if re.search(r"\b(download|save|grab|get)\b", low) and re.search(file_word, low) or re.search(r"\bdownload\b", low):
        folder = _FOLDER_RE.search(low)
        # What to download, when named: "download the installer" → "installer" (matched against link text).
        what = _strip_words(
            _FOLDER_RE.sub(" ", low),
            r"\b(please|download|save|grab|get|this|that|it|the|a|an|my|me|for|file|files|pdfs?|documents?|docs?|link|from|page|here|now)\b",
        )
        return R("download_file", file_type=_file_type(low), folder=folder.group(1).strip() if folder else None,
                 query=what or None)

    # ---- page understanding
    if re.search(r"\b(summari[sz]e|summary|tl;?dr|key (points|takeaways)|main points|gist|recap)\b", low):
        return R("summarize_page", use_current_page=has_page)
    if re.search(r"\bwhat(?:'s| is)\s+(?:this|the)\s+(?:website|site|page|video|repo|repository|article|document|doc|tab)\b", low) or re.search(
        r"\bwhat(?:'s| is)\s+this(?:\s+about)?\s*\??$", low
    ) or re.search(
        r"\b(explain|describe)\s+(this|the)\s+(website|site|page|repo|repository|article)\b", low
    ):
        return R("describe_page", use_current_page=has_page)

    # ---- "Open YouTube" / "go to github.com" (a known site or a domain, not a saved page)
    m = re.match(r"^(?:please\s+|can you\s+|could you\s+)?(?:open|go to|take me to|launch|visit)\s+(.{2,60}?)[.!?]*$", low)
    if m and not re.search(r"\b(saved|yesterday|last|that|again|i was|tab)\b", m.group(1)):
        from .memory_agent import direct_site  # local import: memory_agent imports this module
        if direct_site(m.group(1)):
            return R("open_website", query=m.group(1), use_current_page=False)

    # ---- website memory
    if re.search(r"\b(open|go to|take me to|reopen|bring up|pull up|launch)\b", low) and (
        re.search(r"\b(website|site|page|link|article|bookmark|video|repo|repository|docs|documentation|job|post|blog)\b", low) or _period(low)
    ):
        return R("open_website", query=_topic(text) or _strip_words(low, r"\b(open|go to|take me to|reopen|bring up|pull up|launch|the|that|my|website|site|page|link|article|bookmark|i|saved|visited|was|on|reading|yesterday|today|last week|this week|recently)\b"),
                 period=_period(low), use_current_page=False)
    if re.search(r"\b(show|list|find|search|which|what|where|look up|display)\b", low) and re.search(
        r"\b(websites?|sites?|pages?|bookmarks?|links?|articles?|saved)\b", low
    ):
        return R("search_websites", query=_topic(text), period=_period(low), use_current_page=False)
    if re.search(r"\b(remember|save|bookmark|keep|store|add)\b", low) and (page_ref or re.search(r"\b(this|it)\b", low)):
        return R("save_website", notes=_extract_after(text, [r"\bnote\s*:?\s*", r"\bbecause\s+"]), use_current_page=True)
    if re.search(r"\b(" + "|".join(CONSUME_VERBS) + r")\b", low) and re.search(r"\b(again|later|back)\b", low) and (page_ref or "this" in low):
        return R("create_reminder", title=None, when=text, kind="reminder", use_current_page=True)

    return R("general", use_current_page=has_page and page_ref)


def _lead(low: str) -> int | None:
    m = _LEAD_RE.search(low)
    if not m:
        return None
    n = int(m.group(1)) if m.group(1).isdigit() else _WORDNUM.get(m.group(1), 10)
    return n * 60 if m.group(2).startswith("h") else n


def _file_type(low: str) -> str | None:
    for t in ("pdf", "image", "zip", "doc", "slides", "spreadsheet"):
        if t in low:
            return t
    return None


def _strip_words(low: str, pattern: str) -> str:
    out = re.sub(pattern, " ", low)
    return re.sub(r"\s+", " ", out).strip(" .?!,")


# --------------------------------------------------------------------------- LLM
SYSTEM_PROMPT = """You are the intent router of NOVA, an action-oriented browser assistant.
Classify the user's latest message into exactly ONE intent and extract structured slots.
Return ONLY a JSON object. Never invent facts. Leave unknown slots null.

Intents:
- create_reminder: remind the user at a time (incl. "visit this website again tomorrow"). kind="meeting" for meetings/calls/interviews.
- create_alarm: an alarm ("set an alarm", "wake me up").
- list_reminders, cancel_reminder (reference = which one, e.g. "5 PM" or "dentist"), snooze_reminder (snooze_minutes).
- reschedule_reminder: move an existing reminder/alarm to a new time (reference = which one, when = new time).
- create_task: create a to-do/task (title, when, priority). list_tasks (due_filter: today|tomorrow|upcoming|overdue|all; status_filter: open|completed|all; priority). complete_task / delete_task (reference = task words).
- update_task: change an existing task (reference = which task; when = new due time, priority = new priority, title = new name).
- save_website: save/remember/bookmark the current page. save_note: save selected text/information for later (notes).
- search_websites: find saved websites (query = topic words only, period = past period like "yesterday"/"last week").
- open_website: open a previously saved website (query, period).
- download_file: download a file/PDF from the current page (file_type, folder = folder name if mentioned).
- remember_file: remember a document without downloading. search_files: find saved/downloaded files (query, period).
- summarize_page, describe_page ("what is this website about").
- schedule_event: put an event on the user's Google Calendar ("schedule a meeting tomorrow", "book a call with Sam at 3",
  "add dentist appointment to my calendar"). title = what the event is (null if just "a meeting"), when = verbatim time.
  NOT for reminders: "remind me about my meeting at 5" / "my meeting is at 5, remind me 10 minutes before" → create_reminder kind="meeting".
- help: what NOVA can do. greeting: hello/thanks. general: anything else (questions, chit-chat).

Slot rules:
- when: copy the user's time expression VERBATIM (e.g. "tomorrow at 8 PM", "after two hours", "next Monday"). Do NOT compute dates yourself.
- title: short imperative title without the time, e.g. "Watch this video", "Submit MCA assignment". For an alarm about a meeting: "Client meeting".
- use_current_page: true when the user refers to the current page/video/site ("this", "this website").
- todo: true when the reminder is about doing a task (submit, complete, prepare, pay, call) rather than revisiting content.
- recurrence: daily|weekdays|weekly|monthly only if explicitly recurring.
- lead_minutes: minutes before the event to notify, only if stated ("15 minutes before").

Understanding the user:
- Classify by what the user wants done, not by keywords. Spoken input may be informal, lack punctuation, or contain
  self-corrections ("at 5, no, 6 PM" means 6 PM). Indian English phrasing is common ("do one thing, remind me…",
  "prepone the meeting", "kindly", "tomorrow morning only").
- Use the conversation and "Previous action" to resolve references: after a reminder titled "Call mom",
  "do that tomorrow" → reschedule_reminder with reference="Call mom", when="tomorrow"; "remind me about it later" →
  create_reminder with that title; "open that website again" → open_website with that site's title as query.
- Questions that need an explanation or knowledge ("what is…", "how do I…", "explain…") are general, even if they mention
  a meeting or calendar.

JSON keys: intent, title, when, query, period, priority, recurrence, kind, notes, folder, file_type, reference, lead_minutes, snooze_minutes, due_filter, status_filter, use_current_page, todo."""


def llm_classify(
    router: LLMRouter, message: str, page_meta: dict | None, history: list[dict], now_text: str, preferred: str | None = None,
    last_action: str | None = None,
) -> tuple[IntentResult, str]:
    context_lines = [f"Current local time: {now_text}"]
    if last_action:
        context_lines.append(f"Previous action: {last_action[:300]}")
    if page_meta and page_meta.get("url"):
        # Only metadata — never page body text — reaches the router prompt.
        context_lines.append(
            f"Current page: title={json.dumps((page_meta.get('title') or '')[:200])} "
            f"domain={page_meta.get('domain')} type={page_meta.get('page_type')}"
        )
    else:
        context_lines.append("Current page: none (restricted or not available)")
    msgs = [ChatMessage("system", SYSTEM_PROMPT)]
    for h in history[-6:]:
        msgs.append(ChatMessage("user" if h["role"] == "user" else "assistant", h["content"][:500]))
    msgs.append(ChatMessage("user", "\n".join(context_lines) + f"\n\nUser message: {message}"))

    def validate(text: str):
        data = parse_json_loose(text)
        try:
            IntentResult.model_validate({**data, "source": "llm"})
        except ValidationError as exc:
            raise ValueError(str(exc)[:200]) from exc

    result = router.complete(msgs, json_mode=True, temperature=0.0, validate=validate, preferred=preferred,
                           deadline_s=router.settings.intent_deadline_seconds)
    data = parse_json_loose(result.text)
    return IntentResult.model_validate({**data, "source": "llm"}), result.provider


def classify(
    router: LLMRouter | None, message: str, page_meta: dict | None, history: list[dict], now_text: str, preferred: str | None = None,
    last_action: str | None = None,
) -> tuple[IntentResult, str | None, str | None]:
    """Returns (intent, provider_used, fallback_reason)."""
    has_page = bool(page_meta and page_meta.get("url"))
    if router is not None and router.any_configured():
        try:
            intent, provider = llm_classify(router, message, page_meta, history, now_text, preferred, last_action)
            if not has_page:
                intent.use_current_page = False
            return intent, provider, None
        except AllProvidersFailed as exc:
            log.warning("LLM classification failed, using rules: %s", exc)
            return rule_classify(message, has_page), None, exc.user_message
        except Exception as exc:  # defensive: never let NLU crash the request
            log.exception("Unexpected classifier error: %s", exc)
            return rule_classify(message, has_page), None, "AI classification error"
    return rule_classify(message, has_page), None, "no AI provider configured"
