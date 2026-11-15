"""Calendar Agent: schedules events on the user's Google Calendar.

Conversation contract (every step is a pending step persisted between turns):

  1. Not connected → explain why permission is needed and show a "Connect Google Calendar"
     button (status needs_authorization). Nothing touches Google before the user clicks it —
     or says "yes" to the question — and approves Google's own consent screen.
  2. Ask only for what can't be guessed: the day and time (one question, which also invites
     the topic: "What time tomorrow, and what's the meeting about?"). The title defaults to
     "Meeting" / "Meeting with Rahul" and the length to CALENDAR_DEFAULT_MINUTES; both are
     shown in the confirmation, so a voice user gets there in one answer instead of four.
  3. Summarise and ask for confirmation. Edits are welcome at this point ("make it 3 PM",
     "for an hour", "call it Design Review", "invite sam@example.com") and re-confirmed.
  4. Only after an explicit yes is the event created; the reply carries the Calendar link.
     A network/Google failure creates nothing and keeps the confirmation open so "try again"
     works (with the same idempotency id, so a retry can never double-book).
"""
from __future__ import annotations

import re
import uuid
from dataclasses import replace
from datetime import date, datetime, time, timedelta

from ..integrations import google_calendar as gcal
from ..timeparse import NUMBER_WORDS, humanize, parse_when, strip_spans
from . import reminder_agent
from .base import NO, YES, AgentInput, capitalize, quote, result

GENERIC_TITLES = re.compile(
    r"^(?:(?:a|an|the|my|our|this|that|new)\s+)*(?:meeting|event|call|appointment|session|sync|calendar event|something|it)$", re.I
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_RETRY = re.compile(r"^\s*(try again|retry|again|one more time|please try again)\b", re.I)
_CONNECTED = re.compile(r"\b(connected|done|i('ve| have)? (connected|allowed|approved|signed in)|it'?s connected|finished)\b", re.I)
_EDIT_HINT = re.compile(
    r"\b(change|make it|move|actually|instead|different|rename|call it|name it|invite|add|shorter|longer|earlier|later|prepone|postpone|"
    r"title|name|about)\b", re.I)
_RENAME = re.compile(r"^\s*(?:rename(?: it)?|change the (?:title|name)|give it a (?:title|name)|(?:a )?different (?:title|name))\s*[.!?]*$", re.I)
# Words left over from a time answer that aren't a meeting title ("11 am please", "tomorrow morning then").
_ANSWER_FILLER = re.compile(
    r"\b(?:please|pls|ok(?:ay)?|then|umm*|uh+|hmm+|so|just|sure|yes|yeah|haan|ji|make it|schedule it|book it|set it|"
    r"o'?clock|morning|afternoon|evening|night|tonight|noon|sharp|around|about|ish|is fine|works|would be good|fine|"
    r"at|on|for|in|by|and|the meeting|meeting is|it is|it's|its)\b",
    re.I,
)
_DURATION_PHRASE = re.compile(  # "for an hour", "30 minutes"
    r"\b(?:for|lasting)?\s*(?:an?|one|two|three|half an?|\d+(?:\.\d+)?)\s*(?:hours?|hrs?|minutes?|mins?)(?:\s+and\s+a\s+half)?\b", re.I)
_NUM = r"(\d+(?:\.\d+)?|" + "|".join(sorted((k for k in NUMBER_WORDS if " " not in k), key=len, reverse=True)) + r")"


# ------------------------------------------------------------------ parsing helpers
def _n(tok: str) -> float:
    tok = tok.lower()
    return float(tok) if re.fullmatch(r"\d+(?:\.\d+)?", tok) else float(NUMBER_WORDS.get(tok, 0))


def parse_duration(text: str) -> int | None:
    """Minutes from "for an hour", "30 minutes", "an hour and a half", "1.5 hours", "90 min"."""
    low = " " + text.lower().replace("-", " ") + " "
    if re.search(r"\b(an?|one)\s+hour\s+and\s+a\s+half\b|\b1\.5\s*h", low):
        return 90
    if re.search(r"\bhalf\s+(?:an?\s+)?hour\b", low):
        return 30
    m = re.search(rf"\b{_NUM}\s*(?:hours?|hrs?|h)\b(?:\s*(?:and\s*)?{_NUM}\s*(?:minutes?|mins?))?", low)
    if m:
        mins = int(_n(m.group(1)) * 60 + (_n(m.group(2)) if m.group(2) else 0))
        return mins if 5 <= mins <= 24 * 60 else None
    m = re.search(rf"\b{_NUM}\s*(?:minutes?|mins?|m)\b", low)
    if m:
        mins = int(_n(m.group(1)))
        return mins if 5 <= mins <= 24 * 60 else None
    return None


def _end_time(text: str, start: datetime) -> int | None:
    """"until 3 PM" / "till 4:30" / "from 2 to 3" → duration in minutes relative to start."""
    m = re.search(r"\b(?:until|till|til|to|-|–)\s*(\d{1,2}(?:[:.]\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)\s*$", text.strip(), re.I) or \
        re.search(r"\b(?:until|till|ending at|ends at|end at)\s+(\d{1,2}(?:[:.]\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)", text, re.I)
    if not m:
        return None
    p = parse_when(f"at {m.group(1)}", start)
    if not p:
        return None
    end = datetime.combine(start.date(), p.dt.timetz()).replace(tzinfo=start.tzinfo)
    if end <= start and not re.search(r"[ap]\.?m", m.group(1), re.I) and end.hour < 12:
        end += timedelta(hours=12)  # "from 2 to 4" means PM
    mins = int((end - start).total_seconds() // 60)
    return mins if 5 <= mins <= 24 * 60 else None


def _strip_end_clause(text: str) -> str:
    return re.sub(r"\b(?:until|till|til|ending at|ends at|end at)\s+\d{1,2}(?:[:.]\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?", " ", text, flags=re.I)


def _fmt_duration(mins: int) -> str:
    words = {30: "30 minutes", 45: "45 minutes", 60: "one hour", 90: "an hour and a half", 120: "two hours", 180: "three hours"}
    if mins in words:
        return words[mins]
    if mins % 60 == 0:
        return f"{mins // 60} hours"
    return f"{mins} minutes" if mins < 60 else f"{mins // 60} h {mins % 60} min"


def _clean_title(raw: str | None) -> str | None:
    if not raw:
        return None
    t = raw.strip()
    # "a call called Design review" / "meeting about the budget" → the name itself
    m = re.search(r"\b(?:called|named|titled|about|regarding|on the topic of)\s+(.+)$", t, re.I)
    explicit = bool(m)
    if m:
        t = m.group(1)
    t = re.sub(r"^(?:it'?s|it is|the (?:title|name) is|call it|name it|title it|for|the)\s+", "", t, flags=re.I)
    t = re.sub(r"\b(?:on|to|in|into)\s+(?:my\s+)?(?:google\s+)?calendar\b", "", t, flags=re.I)
    t = re.sub(r"\bwith\s+[\w.+-]+@[\w.-]+(?:\s*(?:,|and)\s*[\w.+-]+@[\w.-]+)*", "", t)
    t = _DURATION_PHRASE.sub("", t)
    t = re.sub(r"\b(tomorrow|today|tonight|(?:this|tomorrow) (?:morning|afternoon|evening)|next \w+)\b", "", t, flags=re.I)
    t = re.sub(r"\s{2,}", " ", t).strip(" .,!?\"'“”")
    # "meeting for [tomorrow at 11] with Rahul" → "meeting with Rahul"
    t = re.sub(r"\b(?:for|at|on|by|from|to|in)\s+(?=(?:with|and)\b)", "", t, flags=re.I)
    # connectors orphaned by removing the time/duration: "meeting for" → "meeting"
    t = re.sub(r"(?:\s+(?:for|at|on|by|from|to|with|and|in))+$", "", t, flags=re.I).strip(" .,!?")
    if not t or (GENERIC_TITLES.match(t) and not explicit) or len(t) > 120:
        return None
    return capitalize(t)


def _title_from_answer(msg: str, now: datetime) -> str | None:
    """The topic inside an answer like "11 am, project discussion" or "at 3, it's about the budget"."""
    if YES.match(msg) or NO.match(msg):
        return None
    text = _DURATION_PHRASE.sub(" ", _strip_end_clause(msg))
    text = _EMAIL.sub(" ", text)
    p = parse_when(text, now)
    if p and p.spans:
        text = strip_spans(text, p.spans)
    text = re.sub(r"\b\d{1,2}(?:[:.]\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?(?![\w])", " ", text, flags=re.I)  # a bare "11"
    explicit = re.search(r"\b(?:called|named|titled|about|regarding)\s+(.+)$", text, re.I)
    if explicit:
        return _clean_title(explicit.group(0))
    if len(re.sub(r"[^a-z]", "", _ANSWER_FILLER.sub(" ", text).lower())) < 3:
        return None
    edge = _ANSWER_FILLER.pattern
    lead, trail = re.compile(rf"^[\s,.;:!-]*{edge}[\s,.;:!-]*", re.I), re.compile(rf"[\s,.;:!-]*{edge}[\s,.;:!?-]*$", re.I)
    prev = None
    while prev != text:  # "then project discussion please" → "project discussion"
        prev = text
        text = trail.sub("", lead.sub("", text.strip()))
    return _clean_title(text)


def _default_title(d: dict) -> str:
    names = d.get("names") or []
    return "Meeting with " + " and ".join(names) if names else "Meeting"


def _names(message: str) -> list[str]:
    """People mentioned as "with Rahul" / "with Priya and Sam" (no email given)."""
    m = re.search(r"\bwith\s+((?:[A-Z][a-z]+)(?:\s*(?:,|and)\s*[A-Z][a-z]+)*)", message)
    if not m:
        return []
    return [n for n in re.split(r"\s*(?:,|and)\s*", m.group(1)) if n and n.lower() not in ("me", "the", "my", "team")]


# ------------------------------------------------------------------ draft state
def _draft(inp: AgentInput) -> dict:
    return dict(inp.extra.get("event") or {"title": None, "date": None, "time": None, "duration": None,
                                          "attendees": [], "names": [], "names_asked": False})


def _start(d: dict, tz) -> datetime | None:
    if not (d.get("date") and d.get("time")):
        return None
    return datetime.combine(date.fromisoformat(d["date"]), time.fromisoformat(d["time"]), tzinfo=tz)


def _apply_when(d: dict, text: str, now: datetime, answering: str | None) -> bool:
    """Update date/time from `text`. Returns True if anything changed."""
    cleaned = _strip_end_clause(text)
    cleaned = re.sub(r"\b(?:for|lasting)\s+(?:an?|one|half an?|\d+(?:\.\d+)?|two|three)\s*(?:hours?|hrs?|minutes?|mins?)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"^(?:no,?\s*)?(?:make it|change (?:it|the time) to|move it to|set it (?:to|for)|actually|instead,?|schedule it (?:for|at))\s*", "", cleaned.strip(), flags=re.I)
    if answering in ("time", "when") and re.match(
            r"\d{1,2}(?:[:.]\d{2})?(?=[\s,.]|$)(?!\s*(?:a\.?m|p\.?m|min|hour|hr|h\b|days?|weeks?))", cleaned.strip()):
        cleaned = f"at {cleaned.strip()}"  # "11" / "11, project discussion" in answer to "what time?"
    p = parse_when(cleaned, now)
    if not p:
        return False
    changed = False
    if p.has_time:
        t = p.dt.time().replace(second=0, microsecond=0)
        if p.ambiguous_meridiem:
            # Meetings happen in working hours: a bare "11" is 11 AM, "3" is 3 PM, "12" is noon.
            h = t.hour % 12
            t = t.replace(hour=h + 12 if 1 <= h <= 6 else (12 if h == 0 else h))
        new_t = t.isoformat(timespec="minutes")
        changed |= new_t != d.get("time")
        d["time"] = new_t
        if p.has_date or not d.get("date"):
            changed |= p.dt.date().isoformat() != d.get("date")
            d["date"] = p.dt.date().isoformat()
    elif p.has_date:
        changed |= p.dt.date().isoformat() != d.get("date")
        d["date"] = p.dt.date().isoformat()
    return changed


def absorb(d: dict, text: str, now: datetime, answering: str | None = None) -> bool:
    """Merge every detail found in `text` into the draft. Returns True if anything changed."""
    changed = _apply_when(d, text, now, answering)
    start = _start(d, now.tzinfo)
    dur = (_end_time(text, start) if start else None) or parse_duration(text)
    if answering == "duration" and dur is None and re.fullmatch(r"\s*\d{1,3}\s*", text):
        dur = int(text)  # "45" in answer to "how long?" → minutes
    if dur and dur != d.get("duration"):
        d["duration"], d["duration_default"], changed = dur, False, True
    emails = [e.lower() for e in _EMAIL.findall(text)]
    if emails:
        d["attendees"] = list(dict.fromkeys((d.get("attendees") or []) + emails))[:20]
        d["names_asked"], changed = True, True
    m = re.search(r"\b(?:call it|name it|title it|rename it to|change the title to|the title is|it'?s called)\s+(.+?)(?:[.!?]|$)", text, re.I)
    if m and (t := _clean_title(m.group(1))):
        d["title"], d["title_default"], changed = t, False, True
    return changed


# ------------------------------------------------------------------ replies
def _auth_needed(inp: AgentInput, d: dict, reconnect: bool = False) -> dict:
    reply = (
        "Your Google Calendar connection has expired, so I need your permission again. Would you like to reconnect it?"
        if reconnect else
        "I can help you schedule that. To add events to your Google Calendar I need your permission first. "
        "Would you like to connect your Google Calendar?"
    )
    return result(
        reply,
        status="needs_authorization",
        actions=[{"type": "connect_calendar", "payload": {"label": "Connect Google Calendar"}}],
        pending=_pending(inp, "auth", d),
    )


def _pending(inp: AgentInput, kind: str, d: dict, missing: str | None = None) -> dict:
    extra = {k: v for k, v in inp.extra.items() if k not in ("history", "event", "calendar_reply")}
    out = {"type": "slot" if missing else kind, "intent": inp.intent.model_dump(), "extra": {**extra, "event": d}}
    if missing:
        out["missing"] = missing
    if kind == "auth":
        out["type"] = "auth"
    return out


def _ask(inp: AgentInput, d: dict, missing: str, question: str, prefix: str = "") -> dict:
    return result(prefix + question, status="needs_clarification", pending=_pending(inp, "slot", d, missing))


def _summary(d: dict, now: datetime) -> str:
    start = _start(d, now.tzinfo)
    s = f"{quote(d['title'])} {humanize(start, now)} for {_fmt_duration(d['duration'])}"
    if d.get("attendees"):
        s += " with " + ", ".join(d["attendees"])
    return s


def _next_step(inp: AgentInput, d: dict, prefix: str = "") -> dict:
    """Ask for the one thing NOVA can't guess (when), default the rest, then confirm."""
    now = inp.deps.now
    about = "" if d.get("title") else ", and what's it about"
    if not d.get("date") and not d.get("time"):
        what = f" {quote(d['title'])}" if d.get("title") else " it"
        return _ask(inp, d, "when", f"When should I schedule{what}{about}?", prefix)
    if not d.get("time"):
        day = humanize(datetime.combine(date.fromisoformat(d["date"]), time(9), tzinfo=now.tzinfo), now).split(" at ")[0]
        if d.get("title"):
            return _ask(inp, d, "time", f"What time {day} should I schedule {quote(d['title'])}?", prefix)
        return _ask(inp, d, "time", f"What time {day}{about}?", prefix)
    start = _start(d, now.tzinfo)
    if start <= now:
        d["time"] = None
        return _ask(inp, d, "when", f"{capitalize(humanize(start, now))} has already passed. When should it be instead?", prefix)
    if not d.get("title"):
        d["title"], d["title_default"] = _default_title(d), True
    if not d.get("duration"):
        d["duration"], d["duration_default"] = inp.deps.settings.calendar_default_minutes, True
    return _confirm(inp, d, prefix)


def _confirm(inp: AgentInput, d: dict, prefix: str = "") -> dict:
    now = inp.deps.now
    d.setdefault("request_id", None)
    d["request_id"] = d["request_id"] or uuid.uuid4().hex  # Calendar event id → idempotent create
    note = ""
    start = _start(d, now.tzinfo)
    try:
        clashes = gcal.find_conflicts(inp.deps.db, inp.deps.user, start, start + timedelta(minutes=d["duration"]))
        if clashes:
            c = clashes[0]
            when = ""
            if c.get("start"):
                when = " at " + datetime.fromisoformat(c["start"]).astimezone(now.tzinfo).strftime("%I:%M %p").lstrip("0")
            note = f" Heads up: you already have {quote(c['summary'], 50)}{when}."
    except gcal.CalendarAuthError:
        return _auth_needed(inp, d, reconnect=True)
    except gcal.CalendarError:
        pass  # clash check is best-effort; the confirmation still works offline
    names = [n for n in d.get("names") or [] if not d.get("attendees")]
    if names:  # "with Rahul" but no email: say so once instead of asking a separate question
        note += f" To send {' and '.join(names)} an invite, just tell me the email."
    reply = f"{prefix}Just to confirm, should I schedule {_summary(d, now)}?{note}"
    suggestions = ["Yes", "Change the time"] + (["Make it 1 hour"] if d.get("duration_default") and d["duration"] < 60 else []) \
        + (["Rename it"] if d.get("title_default") else []) + ["Cancel"]
    return result(reply, status="needs_confirmation", pending=_pending(inp, "confirm", d),
                  suggestions=suggestions, data={"event_draft": _public(d, now)})


def _public(d: dict, now: datetime) -> dict:
    start = _start(d, now.tzinfo)
    return {
        "title": d.get("title"),
        "start": start.isoformat() if start else None,
        "end": (start + timedelta(minutes=d["duration"])).isoformat() if start and d.get("duration") else None,
        "attendees": d.get("attendees") or [],
    }


def _create(inp: AgentInput, d: dict) -> dict:
    deps = inp.deps
    now = deps.now
    start = _start(d, now.tzinfo)
    if start <= now:
        d["time"] = None
        return _ask(inp, d, "when", "That time has passed while we were talking. When should it be instead?")
    end = start + timedelta(minutes=d["duration"])
    deps.trace.append({"node": "tool:create_calendar_event", "detail": "google", "ok": True})
    try:
        ev = gcal.create_event(deps.db, deps.user, title=d["title"], start=start, end=end, tz=deps.user.timezone or "UTC",
                               attendees=d.get("attendees"), request_id=d.get("request_id"))
    except gcal.CalendarAuthError:
        deps.trace[-1]["ok"] = False
        return _auth_needed(inp, d, reconnect=True)
    except gcal.CalendarUnavailable:
        deps.trace[-1]["ok"] = False
        return result("I couldn't reach Google Calendar, so the event was not created. Say “try again” when you're back online, or “cancel”.",
                      status="needs_confirmation", pending=_pending(inp, "confirm", d), suggestions=["Try again", "Cancel"])
    except gcal.CalendarError as exc:
        deps.trace[-1]["ok"] = False
        return result(f"Google Calendar didn't accept that event, so nothing was created. {exc}", status="error")
    t_end = end.strftime("%I:%M %p").lstrip("0")
    reply = f"Done! {quote(d['title'])} is on your Google Calendar for {humanize(start, now)} to {t_end}."
    if d.get("attendees"):
        reply += f" I've sent invitations to {', '.join(d['attendees'])}."
    link = ev.get("htmlLink")
    actions = [{"type": "show_link", "payload": {"url": link, "label": "Open in Google Calendar"}}] if link else []
    return result(reply, actions=actions, data={
        "event": {"id": ev.get("id"), "title": d["title"], "start": start.isoformat(), "end": end.isoformat(),
                  "html_link": link, "attendees": d.get("attendees") or []},
        "last_action": {"intent": "schedule_event", "title": d["title"], "event_id": ev.get("id"), "start": start.isoformat()},
    })


# ------------------------------------------------------------------ entry points
def initial_draft(inp: AgentInput) -> dict:
    d = _draft(inp)
    now = inp.deps.now
    it = inp.intent
    if it.when:
        _apply_when(d, it.when, now, None)
    if not d.get("time") or not d.get("date"):
        _apply_when(d, inp.message, now, None)
    start = _start(d, now.tzinfo)
    d["duration"] = d.get("duration") or (_end_time(inp.message, start) if start else None) or parse_duration(inp.message)
    d["attendees"] = list(dict.fromkeys(e.lower() for e in _EMAIL.findall(inp.message)))
    d["names"] = [] if d["attendees"] else _names(inp.message)
    d["names_asked"] = bool(d["attendees"])
    d["title_default"] = d["duration_default"] = False
    raw = _strip_end_clause(it.title or "")
    # The classifier's title may still contain the time ("Project sync tomorrow at 11").
    p = parse_when(raw, now) if raw else None
    if p and p.spans:
        raw = strip_spans(raw, p.spans)
    d["title"] = _clean_title(raw)
    return d




def _is_topic(message: str) -> bool:
    """A short reply that names the meeting ("project discussion") rather than starting a new request."""
    from .intents import rule_classify  # local import: intents → graph → this module

    msg = message.strip()
    if not msg or len(msg.split()) > 8 or msg.endswith("?") or YES.match(msg) or NO.match(msg):
        return False
    if re.match(r"^(what|when|where|who|why|how|can|could|will|would|is|are|do|does|show|open|find|search|remind|set|create)\b", msg, re.I):
        return False
    return rule_classify(msg).intent == "general"


def absorbs(message: str, pending: dict, now: datetime) -> bool:
    """Would this reply be handled by the pending calendar step (vs. being a new request)?"""
    ptype = pending.get("type")
    if YES.match(message) or NO.match(message) or _RETRY.match(message):
        return True
    if ptype == "auth":
        return bool(_CONNECTED.search(message)) or len(message.split()) <= 4
    if ptype == "slot" and pending.get("missing") in ("title", "attendees"):
        return True
    d = dict((pending.get("extra") or {}).get("event") or {})
    if ptype == "confirm":
        if _EDIT_HINT.search(message) or _RENAME.match(message):
            return True
        if d.get("title_default") and _is_topic(message):
            return True
    if ptype == "slot" and pending.get("missing") in ("when", "time") and not d.get("title") and _is_topic(message):
        return True  # answered "what's it about" before the time
    return absorb(d, message, now, pending.get("missing"))


def _reminder_instead(inp: AgentInput, reason: str) -> dict:
    """No Google Calendar on this server: a meeting reminder still gets the user what they asked for."""
    d = initial_draft(inp)
    it = inp.intent.model_copy(update={"intent": "create_reminder", "kind": "meeting", "title": d.get("title"),
                                       "when": inp.intent.when or inp.message, "datetime_iso": None, "source": "rules"})
    out = reminder_agent.create(replace(inp, intent=it, extra={}))
    out["reply"] = f"{reason} {out['reply']}"
    return out


_CONNECT_CMD = re.compile(r"\b(connect|open|sign ?in|log ?in|link|allow|go ahead|let'?s do it)\b", re.I)


def _connect_now(inp: AgentInput, d: dict) -> dict:
    """The user said yes: open Google's consent page right away (they still approve on Google's own screen)."""
    return result(
        "Opening Google's sign-in page now. Choose your account and allow access — I'll carry on right here.",
        status="needs_authorization",
        actions=[{"type": "connect_calendar", "payload": {"label": "Connect Google Calendar", "auto": True}}],
        pending=_pending(inp, "auth", d),
    )


def run(inp: AgentInput) -> dict:
    deps = inp.deps
    tz_now = deps.now
    reply_to = inp.extra.pop("calendar_reply", None)  # (pending type, missing slot) when resuming
    msg = inp.message.strip()
    if not gcal.configured(deps.settings):
        if reply_to is not None and NO.match(msg):
            return result("Okay, I won't schedule it.")
        return _reminder_instead(inp, "Google Calendar isn't set up on this NOVA server, so I'll set a meeting reminder instead.")

    if reply_to is None:  # brand-new request
        d = initial_draft(inp)
        if not gcal.is_connected(deps.db, deps.user):
            return _auth_needed(inp, d)
        return _next_step(inp, d, prefix="Sure. " if not (d.get("date") and d.get("time")) else "")

    ptype, missing = reply_to
    d = _draft(inp)

    if ptype == "auth":
        if gcal.is_connected(deps.db, deps.user):
            return _next_step(inp, d, prefix="Your Google Calendar is connected. ")
        if NO.match(msg):
            return result("Okay, I won't connect your calendar. If you like, I can set a reminder for the meeting instead — "
                          "just say “remind me about the meeting” with a time.")
        if _CONNECTED.search(msg) and not re.match(r"^\s*(?:ok(?:ay)?|yes|sure)\b", msg, re.I):
            return result(
                "I don't see the connection yet. Finish on Google's page and I'll continue automatically — "
                "or say “connect” and I'll open it again.",
                status="needs_authorization",
                actions=[{"type": "connect_calendar", "payload": {"label": "Connect Google Calendar"}}],
                pending=_pending(inp, "auth", d),
            )
        if YES.match(msg) or _CONNECT_CMD.search(msg):
            return _connect_now(inp, d)
        absorb(d, msg, tz_now)  # details given while waiting ("at 11 am") are kept for later
        return result(
            "Got it. To add it to your Google Calendar I still need your permission — say “yes” or click “Connect Google Calendar”.",
            status="needs_authorization",
            actions=[{"type": "connect_calendar", "payload": {"label": "Connect Google Calendar"}}],
            pending=_pending(inp, "auth", d),
        )

    if not gcal.is_connected(deps.db, deps.user):
        return _auth_needed(inp, d, reconnect=True)

    if ptype == "confirm":
        if _RENAME.match(msg):
            d["request_id"] = None
            return _ask(inp, d, "title", "Sure — what should I call it?")
        changed = absorb(d, msg, tz_now)
        if changed:
            d["request_id"] = None  # different event now
            return _next_step(inp, d, prefix="Got it. ")
        if YES.match(msg) or _RETRY.match(msg):
            return _create(inp, d)
        if NO.match(msg):
            return result("Okay, I won't schedule it.")
        if re.search(r"\b(change|different|another)\b.*\btime\b", msg, re.I):
            d["time"] = None
            d["request_id"] = None
            return _ask(inp, d, "time", "Sure — what time should it be?")
        if d.get("title_default") and _is_topic(msg) and (t := _title_from_answer(msg, tz_now) or _clean_title(msg)):
            d["title"], d["title_default"], d["request_id"] = t, False, None
            return _next_step(inp, d, prefix="Got it. ")
        return _confirm(inp, d, prefix="Sorry, I didn't catch that. ")

    # answering a question
    if NO.match(msg) and missing != "attendees" and len(msg.split()) <= 3:
        return result("Okay, I won't schedule it.")
    if missing == "title":
        if YES.match(msg) and d.get("title"):
            return _next_step(inp, d)  # "ok" → keep the current name
        absorb(d, msg, tz_now, missing)  # "Design review at 3 for an hour" answers several things
        text = _DURATION_PHRASE.sub(" ", _strip_end_clause(msg))
        p = parse_when(text, tz_now)
        if p and p.spans:
            text = strip_spans(text, p.spans)
        title = None if YES.match(msg) else _clean_title(text)
        if title:
            d["title"], d["title_default"] = title, False
        elif not d.get("title"):
            return _ask(inp, d, "title", "What should I call it? For example “Project discussion”.")
    elif missing == "attendees":
        d["names_asked"] = True
        absorb(d, msg, tz_now, missing)
    else:
        changed = absorb(d, msg, tz_now, missing)
        if missing in ("when", "time") and (not d.get("title") or d.get("title_default")):
            if t := _title_from_answer(msg, tz_now):  # "11 am, project discussion"
                d["title"], d["title_default"], changed = t, False, True
        if not changed:
            q = {"when": "Sorry, I didn't catch the time. When should I schedule it — for example “tomorrow at 11 AM”?",
                 "time": "Sorry, what time should it start? For example “11 AM” or “3:30 PM”.",
                 "duration": "How long should it be? For example “30 minutes” or “one hour”."}.get(missing, "Sorry, could you say that again?")
            return _ask(inp, d, missing, q)
    return _next_step(inp, d)
