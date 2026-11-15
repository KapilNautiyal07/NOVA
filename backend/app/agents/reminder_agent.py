"""Reminder Agent: creates reminders/alarms/meetings, resolves dates, snoozes and cancels."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ..timeparse import ParsedWhen, humanize, parse_recurrence, parse_when, strip_spans
from .base import AgentInput, ask_choice, ask_confirm, ask_slot, capitalize, quote, result, ser_reminder, ser_task

REMINDER_ACTIONS = [{"type": "sync_reminders", "payload": {}}]
_PAGE_WORDS = re.compile(r"\b(this|the current|current|that)\s+(website|site|page|video|article|link|url|repo|repository|post|job|listing|doc|documentation|tab|thread|course|tutorial|product)\b", re.I)
_FILLER = re.compile(r"^(?:to|about|of|that|for|me to|me)\s+", re.I)
_TIME_TAIL = re.compile(
    r"\b(?:after|in|within)\s+(?:a|an|one|two|three|four|five|six|ten|twenty|thirty|few|couple of|\d+)\s*(?:minutes?|mins?|hours?|hrs?|days?|weeks?|months?)\b"
    r"|\b(?:every\s*day|daily|weekly|monthly|every\s+\w+)\b|\b\d+\s*(?:min(?:ute)?s?|hours?)\s+before\b",
    re.I,
)


def resolve_when(inp: AgentInput, prefer_am: bool = False, use_message: bool = True) -> ParsedWhen | None:
    """`use_message=False` for edits, where the message also names the *old* time
    ("move my 5 PM reminder to 6 PM") and only the extracted new time may be used."""
    now = inp.deps.now
    for candidate in (inp.intent.when, inp.message if use_message else None):
        if candidate:
            parsed = parse_when(candidate, now, prefer_am=prefer_am)
            if parsed:
                return parsed
    if inp.intent.datetime_iso:  # model-computed fallback, only if our parser found nothing
        try:
            dt = datetime.fromisoformat(inp.intent.datetime_iso)
            dt = dt.replace(tzinfo=now.tzinfo) if dt.tzinfo is None else dt.astimezone(now.tzinfo)
            return ParsedWhen(dt, True, True)
        except ValueError:
            return None
    return None


def build_title(inp: AgentInput, parsed: ParsedWhen | None, default: str) -> str:
    title = (inp.intent.title or "").strip()
    if title and parsed and inp.intent.source == "rules":
        # rule-extracted titles still contain the time expression
        rel = parse_when(title, inp.deps.now)
        if rel:
            title = strip_spans(title, rel.spans)
    title = _TIME_TAIL.sub("", title)
    title = re.sub(r"\b(tomorrow|today|tonight|next \w+|this (?:morning|evening|afternoon))\b", "", title, flags=re.I)
    title = _FILLER.sub("", title.strip(" .,!?"))
    title = re.sub(r"\s{2,}", " ", title).strip(" .,!?")
    # Connectors orphaned by removing the time: "fix the login bug by" → "fix the login bug"
    title = re.sub(r"(?:\s+(?:by|at|on|before|for|due|until|from|around|in))+$", "", title, flags=re.I).strip(" .,!?")
    page = inp.page
    if page.get("url") and (inp.intent.use_current_page or _PAGE_WORDS.search(title)):
        page_title = quote(page.get("title"), 60)
        if not title or re.fullmatch(r"(this|it|that)", title, re.I):
            return f"Revisit {page_title}"
        replaced = _PAGE_WORDS.sub(page_title, title, count=1)
        if replaced == title:
            replaced = re.sub(r"\b(this|it)\s*$", page_title, title, count=1, flags=re.I)
        if replaced == title and inp.intent.use_current_page and page_title.strip("“”") not in title:
            replaced = f"{title}: {page_title}" if title else f"Revisit {page_title}"
        return capitalize(replaced)
    if title.lower().startswith("my "):
        title = title[3:]
    return capitalize(title) if title else default


def _local(dt: datetime, inp: AgentInput) -> datetime:
    return dt.astimezone(inp.deps.now.tzinfo)


def create(inp: AgentInput) -> dict:
    it = inp.intent
    deps = inp.deps
    kind = "alarm" if it.intent == "create_alarm" else (it.kind or "reminder")
    page = inp.page
    uses_page = bool(page.get("url")) and it.use_current_page
    # "Wake me up at 6:30" means the morning — don't ask.
    parsed = resolve_when(inp, prefer_am=bool(re.search(r"\bwake\b", inp.message, re.I)))
    if "meridiem_answer" in inp.extra and parsed:
        parsed.ambiguous_meridiem = False
    recurrence = it.recurrence or parse_recurrence(inp.message)

    website_id = inp.extra.get("website_id")
    saved_note = ""
    if uses_page and not website_id:
        site, created = inp.tools.call("save_website", url=page["url"], title=page.get("title") or "")
        website_id = site.id
        saved_note = "I've saved this website. " if created else ""

    label = {"alarm": "the alarm", "meeting": "the meeting reminder"}.get(kind, "you")
    if parsed is None:
        q = "What time should I set the alarm for?" if kind == "alarm" else "When would you like me to remind you?"
        return ask_slot(saved_note + q, inp, "when", website_id=website_id)
    if not parsed.has_time:
        if kind != "reminder" or uses_page or not it.todo:
            day = humanize(parsed.dt, deps.now).split(" at ")[0]
            return ask_slot(f"{saved_note}What time {day} should I {'set ' + label if kind != 'reminder' else 'remind you'}?",
                            inp, "time", website_id=website_id, date_text=it.when or inp.message)
        # to-do style ("remind me to submit X next Monday") → morning reminder by default
    if parsed.ambiguous_meridiem and kind in ("alarm", "meeting") and not inp.extra.get("meridiem_answer"):
        h = parsed.dt.hour % 12 or 12
        hm = f"{h}:{parsed.dt.minute:02d}" if parsed.dt.minute else str(h)
        return ask_slot(f"Is that {hm} AM or {hm} PM?", inp, "meridiem", website_id=website_id,
                        date_text=it.when or inp.message)
    if parsed.dt <= deps.now:
        return ask_slot(f"{humanize(parsed.dt, deps.now).capitalize()} has already passed. When should it be instead?",
                        inp, "when", website_id=website_id)

    default_title = {"alarm": "Alarm", "meeting": "Meeting"}.get(kind, "Reminder")
    title = build_title(inp, parsed, default_title)
    offsets = [0]
    if it.lead_minutes:
        offsets = [it.lead_minutes, 0]
    elif kind == "meeting":
        offsets = [10, 0]

    task = None
    if kind == "reminder" and it.todo:
        task = inp.tools.call(
            "create_task",
            title=title,
            due_at=parsed.utc,
            due_has_time=parsed.has_time,
            priority=it.priority or "medium",
            recurrence=recurrence,
            related_url=page.get("url") if uses_page else None,
            related_title=page.get("title") if uses_page else None,
        )
    rem = inp.tools.call(
        "create_reminder",
        title=title,
        kind=kind,
        scheduled_at=parsed.utc,
        timezone=deps.user.timezone,
        recurrence=recurrence,
        notify_offsets=offsets,
        related_url=page.get("url") if uses_page else None,
        related_title=page.get("title") if uses_page else None,
        task_id=task.id if task else None,
        website_id=website_id,
    )
    when_text = humanize(rem.scheduled_at, deps.now)
    rec_text = f", repeating {recurrence}" if recurrence else ""
    if kind == "alarm":
        reply = f"Alarm set for {when_text}{rec_text}" + (f" — {title}." if title != "Alarm" else ".")
        reply += " Keep Chrome running so it can ring."
    elif kind == "meeting":
        lead = offsets[0]
        reply = f"Done! I'll remind you about {title} {when_text}{rec_text}" + (f", and {lead} minutes before." if lead else ".")
    else:
        reply = f"{saved_note}Done! I'll remind you {when_text}{rec_text}: {title}."
    pending = None
    if not parsed.has_time:
        reply += " (I picked 9:00 AM — tell me if you'd prefer another time.)"
        # Let a bare follow-up like "6 PM" / "make it 10 AM" move what was just created.
        day = rem.scheduled_at.astimezone(deps.now.tzinfo).date().isoformat()
        pending = {
            "type": "slot", "missing": "adjust_time",
            "intent": it.model_copy(update={"intent": "reschedule_reminder", "when": None, "datetime_iso": None}).model_dump(),
            "extra": {"chosen_id": rem.id, "date_text": day},
        }
    data = {"reminder": ser_reminder(rem)}
    if task:
        data["task"] = ser_task(task)
        reply += " I've also added it to your tasks."
    return result(reply, actions=REMINDER_ACTIONS, data=data, pending=pending)


def list_(inp: AgentInput) -> dict:
    it = inp.intent
    now = inp.deps.now
    start = end = None
    if it.due_filter in ("today", "tomorrow"):
        day = now.date() + timedelta(days=1 if it.due_filter == "tomorrow" else 0)
        start = datetime.combine(day, datetime.min.time()).replace(tzinfo=now.tzinfo)
        end = start + timedelta(days=1)
    statuses = ["completed", "fired"] if it.status_filter == "completed" else ["pending", "snoozed"]
    rems = inp.tools.call("list_reminders", statuses=statuses, start=start, end=end, limit=50)
    if it.status_filter != "completed":
        rems = sorted(rems, key=lambda r: r.next_fire_at)
    scope = f" for {it.due_filter}" if it.due_filter in ("today", "tomorrow") else ""
    if not rems:
        return result(f"You have no {'completed' if it.status_filter == 'completed' else 'upcoming'} reminders{scope}.",
                      data={"reminders": []}, actions=[{"type": "show_view", "payload": {"view": "reminders"}}])
    lines = [f"• {r.title} — {humanize(r.next_fire_at, now)}" + (" (snoozed)" if r.status == "snoozed" else "") for r in rems[:5]]
    more = f"\n…and {len(rems) - 5} more." if len(rems) > 5 else ""
    head = f"You have {len(rems)} {'completed' if it.status_filter == 'completed' else 'upcoming'} reminder{'s' if len(rems) != 1 else ''}{scope}:"
    return result(f"{head}\n" + "\n".join(lines) + more, data={"reminders": [ser_reminder(r) for r in rems]},
                  actions=[{"type": "show_view", "payload": {"view": "reminders"}}])


def _target(inp: AgentInput, verb: str) -> tuple[object | None, dict | None]:
    """Resolve which reminder the user means. Returns (reminder, clarification_result)."""
    chosen = inp.extra.get("chosen_id")
    if chosen:
        rems = inp.tools.call("list_reminders", statuses=["pending", "snoozed", "fired"], limit=500)
        rem = next((r for r in rems if r.id == chosen), None)
        return rem, None if rem else result("That reminder no longer exists.", status="error")
    ref = inp.intent.reference or ""
    hits = inp.tools.call("find_reminders", reference=ref) if ref else []
    if not ref and verb == "snooze":
        recent = inp.tools.call("list_reminders", statuses=["fired", "pending", "snoozed"], limit=500)
        fired = [r for r in recent if r.last_fired_at]
        hits = sorted(fired, key=lambda r: r.last_fired_at, reverse=True)[:1]
    if not hits:
        upcoming = inp.tools.call("list_reminders", statuses=["pending", "snoozed"], limit=5)
        if not upcoming:
            return None, result("You don't have any active reminders.", data={"reminders": []})
        opts = [{"id": r.id, "label": f"{r.title} — {humanize(r.next_fire_at, inp.deps.now)}"} for r in upcoming]
        return None, ask_choice(f"Which reminder should I {verb}?", inp, opts)
    if len(hits) > 1:
        opts = [{"id": r.id, "label": f"{r.title} — {humanize(r.next_fire_at, inp.deps.now)}"} for r in hits[:5]]
        return None, ask_choice(f"I found {len(hits)} matching reminders. Which one should I {verb}?", inp, opts)
    return hits[0], None


def cancel(inp: AgentInput) -> dict:
    rem, clar = _target(inp, "cancel")
    if clar:
        return clar
    when = humanize(rem.next_fire_at, inp.deps.now)
    if not inp.confirmed:
        return ask_confirm(f"Cancel {quote(rem.title)} scheduled {when}?", inp, chosen_id=rem.id)
    rem = inp.tools.call("cancel_reminder", id=rem.id)
    return result(f"Cancelled {quote(rem.title)} ({when}).", actions=REMINDER_ACTIONS, data={"reminder": ser_reminder(rem)})


def snooze(inp: AgentInput) -> dict:
    rem, clar = _target(inp, "snooze")
    if clar:
        return clar
    minutes = inp.intent.snooze_minutes or 10
    rem = inp.tools.call("snooze_reminder", id=rem.id, minutes=minutes)
    until = humanize(rem.snoozed_until.astimezone(timezone.utc), inp.deps.now)
    return result(f"Snoozed {quote(rem.title)} for {minutes} minutes — I'll remind you {until}.",
                  actions=REMINDER_ACTIONS, data={"reminder": ser_reminder(rem)})


def reschedule(inp: AgentInput) -> dict:
    rem, clar = _target(inp, "move")
    if clar:
        return clar
    now = inp.deps.now
    parsed = resolve_when(inp, use_message=False)
    if parsed is None:
        return ask_slot(f"When should I move {quote(rem.title)} to?", inp, "when", chosen_id=rem.id)
    target = parsed.dt
    if not parsed.has_time:  # "move it to Friday" keeps the original time of day
        old_local = rem.scheduled_at.astimezone(now.tzinfo)
        target = target.replace(hour=old_local.hour, minute=old_local.minute)
    if target <= now:
        return ask_slot(f"{humanize(target, now).capitalize()} has already passed. When should it be instead?",
                        inp, "when", chosen_id=rem.id)
    rem = inp.tools.call("reschedule_reminder", id=rem.id, scheduled_at=target.astimezone(timezone.utc))
    data = {"reminder": ser_reminder(rem)}
    if rem.task_id:
        task = inp.tools.call("update_task", id=rem.task_id, due_at=rem.scheduled_at, due_has_time=True)
        data["task"] = ser_task(task)
    return result(f"Done — moved {quote(rem.title)} to {humanize(rem.scheduled_at, now)}.",
                  actions=REMINDER_ACTIONS, data=data)


def run(inp: AgentInput) -> dict:
    handlers = {
        "create_reminder": create, "create_alarm": create, "list_reminders": list_,
        "cancel_reminder": cancel, "snooze_reminder": snooze, "reschedule_reminder": reschedule,
    }
    return handlers[inp.intent.intent](inp)
