"""Follow-up commands that only make sense against what NOVA just did.

    "Do that tomorrow" / "move it to 6 PM"     → reschedule the reminder/task just created
    "Remind me about that later" / "…at 5"     → a reminder titled after the last thing
    "Open that website again"                  → reopen the last website NOVA saved/opened

The "last action" is stored in the conversation state after every successful turn (see
runner.py) and is also summarised for the LLM classifier, so freer phrasings are covered by
the model while these common ones are deterministic and testable.
"""
from __future__ import annotations

import re

from ..timeparse import parse_when
from .intents import IntentResult

_REF = r"(?:that|it|this|the same|same)"
_RESCHEDULE = re.compile(
    rf"^(?:please\s+)?(?:(?:do|move|push|shift|change|make|reschedule|postpone|set)\s+{_REF}(?:\s+one)?\s*(?:to|for|at|till|until)?\s+|{_REF}\s+(?:should be|for)\s+)(?P<when>.+?)[.!?]*$",
    re.I,
)
_REMIND_THAT = re.compile(rf"^(?:please\s+)?remind me(?:\s+(?:about|of|to do)\s+{_REF})?(?P<when>\s+.+?)?[.!?]*$", re.I)
_OPEN_AGAIN = re.compile(
    rf"^(?:please\s+)?(?:open|reopen|show|go to|take me to|bring up|pull up)\s+(?:{_REF}\s+)?(?:website|site|page|link|one|video|article)?\s*(?:again|back up)?[.!?]*$",
    re.I,
)


def summarize_last(last: dict | None) -> str | None:
    if not last:
        return None
    bits = [last.get("intent", "")]
    for k in ("title", "when", "website_title", "website_url"):
        if last.get(k):
            bits.append(f"{k}={last[k]!r}")
    return " ".join(b for b in bits if b)


def resolve(message: str, last: dict | None, now) -> tuple[IntentResult, dict] | None:
    """Returns (intent, extra) for a recognised follow-up, else None."""
    if not last:
        return None
    # Spoken fillers in front of the real request: "Actually, do that tomorrow", "OK so move it to 5"
    msg = re.sub(r"^(?:(?:actually|ok(?:ay)?|no|sorry|wait|hmm+|um+|uh+|so|and|then|oh|please|nova)[,.!\s]+)+", "", message.strip(), flags=re.I)
    low = msg.lower()
    intent = last.get("intent") or ""

    m = _OPEN_AGAIN.match(msg)
    if m and re.search(r"\b(again|that|same|back)\b", low) and last.get("website_url"):
        return IntentResult(intent="open_website", query=last.get("website_title"), source="rules"), {
            "direct_url": last["website_url"], "direct_title": last.get("website_title")}

    m = _RESCHEDULE.match(msg)
    if m and parse_when(m.group("when"), now):
        when = m.group("when")
        if intent in ("create_reminder", "create_alarm", "reschedule_reminder", "snooze_reminder") and last.get("reminder_id"):
            return IntentResult(intent="reschedule_reminder", reference=last.get("title"), when=when, source="rules"), {
                "chosen_id": last["reminder_id"]}
        if intent in ("create_task", "update_task") and last.get("task_id"):
            return IntentResult(intent="update_task", reference=last.get("title"), when=when, source="rules"), {
                "chosen_id": last["task_id"]}

    m = _REMIND_THAT.match(msg)
    refers_back = re.search(rf"\b(?:about|of)\s+{_REF}\b|\blater\b|^remind me\s+(?:in|at|after|tomorrow|tonight|on)\b", low)
    if m and refers_back and last.get("title") and not re.search(r"\bremind me\s+(?:to|that)\s+(?!do (?:that|it))", low):
        return IntentResult(intent="create_reminder", title=last["title"], when=(m.group("when") or "").strip() or None,
                            kind="reminder", source="llm"), {}
    return None


def last_from(intent: str, data: dict, prev: dict | None) -> dict | None:
    """What to remember about this turn for the next one (None keeps the previous)."""
    if "last_action" in data:
        return data["last_action"]
    if r := data.get("reminder"):
        return {"intent": intent, "title": r.get("title"), "reminder_id": r.get("id"), "when": r.get("scheduled_at"),
                "website_url": r.get("related_url") or (prev or {}).get("website_url"),
                "website_title": r.get("related_title") or (prev or {}).get("website_title")}
    if t := data.get("task"):
        return {"intent": intent, "title": t.get("title"), "task_id": t.get("id"), "when": t.get("due_at")}
    if w := data.get("website"):
        return {"intent": intent, "title": w.get("title"), "website_url": w.get("url"), "website_title": w.get("title")}
    if f := data.get("file"):
        return {"intent": intent, "title": f.get("file_name")}
    return None
