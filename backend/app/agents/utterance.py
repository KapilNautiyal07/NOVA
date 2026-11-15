"""Normalise what users actually type or say before anything else reads it.

Real messages arrive as "hey nova schedule my meeting for tommorow 11 baje". Every agent
(intent rules, the LLM, the time parser, title cleanup) should see the same clean sentence,
so this runs once, where the message enters the assistant.
"""
from __future__ import annotations

import re

# Typed "Hey NOVA, …" (or a transcript that kept the wake word) is an address, not content.
_WAKE = re.compile(r"^\s*(?:(?:hey|hi|hello|ok|okay)[\s,]+)?(?:nova|noah|nover|knova)\b[\s,.!?:-]*", re.I)

_REPLACEMENTS: list[tuple[re.Pattern[str], str]] = [
    # common spellings of "tomorrow"
    (re.compile(r"\b(?:tomm?orr?ow|tomorow|tommorrow|tomorrw|tmrw|tmrow|tmorrow|2morr?ow|2moro)\b", re.I), "tomorrow"),
    (re.compile(r"\b(?:tday|2day)\b", re.I), "today"),
    # Hinglish time words: "11 baje" = 11 o'clock, "subah" = morning, …
    (re.compile(r"\b(\d{1,2}(?:[:.]\d{2})?)\s*baje\b", re.I), r"at \1"),
    (re.compile(r"\bsubah\b", re.I), "morning"),
    (re.compile(r"\b(?:dopahar|dopehar|dophar)\b", re.I), "afternoon"),
    (re.compile(r"\b(?:shaam|sham)\b", re.I), "evening"),
    (re.compile(r"\braat\b", re.I), "night"),
    (re.compile(r"\b(?:mtg|meetng|meating|meetting)\b", re.I), "meeting"),
    (re.compile(r"\bshedule\b|\bschedual\b|\bscedule\b|\bschdule\b", re.I), "schedule"),
]


def normalize(message: str) -> str:
    text = (message or "").strip()
    stripped = _WAKE.sub("", text, count=1).strip()
    if stripped:  # "Hey NOVA" on its own stays as-is (a greeting)
        text = stripped[:1].upper() + stripped[1:] if text[:1].isupper() else stripped
    for pattern, repl in _REPLACEMENTS:
        text = pattern.sub(repl, text)
    text = re.sub(r"\bat\s+at\b", "at", text, flags=re.I)  # "at 11 baje" → "at at 11"
    return re.sub(r"\s{2,}", " ", text).strip()
