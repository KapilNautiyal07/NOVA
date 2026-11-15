"""Shared helpers for specialist agents."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..schemas import FileOut, ReminderOut, TaskOut, WebsiteOut
from .intents import IntentResult
from .tools import Deps, ToolBox


@dataclass
class AgentInput:
    intent: IntentResult
    message: str
    page: dict  # profile from the Browser Context Agent
    deps: Deps
    tools: ToolBox
    confirmed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)  # carried over from a pending step


def result(
    reply: str,
    *,
    status: str = "done",
    actions: list[dict] | None = None,
    data: dict | None = None,
    pending: dict | None = None,
    suggestions: list[str] | None = None,
) -> dict:
    return {
        "reply": reply,
        "status": status,
        "client_actions": actions or [],
        "data": data or {},
        "pending": pending,
        "suggestions": suggestions or [],
    }


# Page fields kept with a pending step so "this website" still means the original page on the
# follow-up turn, even if the user has navigated away. Page *text* is deliberately excluded:
# pending state is persisted in the database and page content must never be stored.
_SNAPSHOT_KEYS = ("url", "title", "domain", "page_type", "content_type", "description", "file_links", "restricted")


def page_snapshot(page: dict) -> dict | None:
    if not page.get("url"):
        return None
    return {k: page.get(k) for k in _SNAPSHOT_KEYS}


def _pending_extra(inp: AgentInput, extra: dict) -> dict:
    out = {**inp.extra, **extra}
    out.pop("history", None)
    snap = page_snapshot(inp.page)
    if snap:
        out["page"] = snap
    return out


def ask_slot(reply: str, inp: AgentInput, missing: str, **extra) -> dict:
    return result(
        reply,
        status="needs_clarification",
        pending={"type": "slot", "intent": inp.intent.model_dump(), "missing": missing, "extra": _pending_extra(inp, extra)},
    )


def ask_confirm(reply: str, inp: AgentInput, **extra) -> dict:
    return result(
        reply,
        status="needs_confirmation",
        pending={"type": "confirm", "intent": inp.intent.model_dump(), "extra": _pending_extra(inp, extra)},
        suggestions=["Yes", "No"],
    )


def ask_choice(reply: str, inp: AgentInput, options: list[dict], **extra) -> dict:
    numbered = "\n".join(f"{i}. {o['label']}" for i, o in enumerate(options, 1))
    return result(
        f"{reply}\n{numbered}",
        status="needs_clarification",
        pending={"type": "choose", "intent": inp.intent.model_dump(), "options": options, "extra": _pending_extra(inp, extra)},
        suggestions=[str(i) for i in range(1, min(len(options), 4) + 1)],
    )


# Ordinal words are checked before cardinals so "the second one" resolves to 2, not 1.
ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
            "fifth": 5, "5th": 5, "last": -1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def resolve_choice(message: str, options: list[dict]) -> dict | None:
    low = message.lower().strip()
    m = re.search(r"\b(\d+)\b", low)
    if m and 1 <= int(m.group(1)) <= len(options):
        return options[int(m.group(1)) - 1]
    for word, idx in ORDINALS.items():
        if re.search(rf"\b{word}\b", low):
            return options[idx if idx < 0 else idx - 1] if abs(idx) <= len(options) else None
    hits = [o for o in options if low and low in o["label"].lower()]
    return hits[0] if len(hits) == 1 else None


# Includes the everyday Indian-English / Hinglish answers people actually give ("haan", "theek hai", "kar do").
YES = re.compile(
    r"^\s*(y|yes|yeah|yea|yep|yup|ya|yah|sure|ok|okay|k|confirm|confirmed|do it|go ahead|please do|proceed|absolutely|correct|"
    r"right|perfect|sounds good|looks good|that'?s (?:right|correct|fine|good)|fine|alright|all right|great|book it|add it|"
    r"schedule it|go for it|of course|definitely|ha+n?|hanji|haan ji|ji|ji haan|jee|theek hai|thik hai|thik|theek|"
    r"kar do|kardo|karo|chalo|bilkul|done)\b",
    re.I,
)
NO = re.compile(
    r"^\s*(n|no|nope|nah|na|cancel|stop|don't|do not|never ?mind|abort|forget it|not now|leave it|nahi|nahin|nai|mat karo|"
    r"rehne do|rahne do|chhodo|chodo)\b",
    re.I,
)


def ser_reminder(r) -> dict:
    return ReminderOut.model_validate(r).model_dump(mode="json")


def ser_task(t) -> dict:
    return TaskOut.model_validate(t).model_dump(mode="json")


def ser_site(s) -> dict:
    return WebsiteOut.model_validate(s).model_dump(mode="json")


def ser_file(f) -> dict:
    return FileOut.model_validate(f).model_dump(mode="json")


def quote(title: str | None, limit: int = 70) -> str:
    title = (title or "").strip()
    if "“" in title:  # already contains a quoted page title
        return title
    if len(title) > limit:
        title = title[: limit - 1].rstrip() + "…"
    return f"“{title}”"


def capitalize(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text
