"""Turn an on-screen reply into natural spoken text.

Replies are written for the screen (bullets, links, emoji, quotes, numbered options). Read
verbatim they sound robotic ("bullet, open paren, h t t p s colon…"), so the spoken version:
  * drops markdown, code, JSON, URLs, emoji and decorative symbols,
  * turns bullet/numbered lists into short spoken sentences ("Option 1: …"),
  * keeps it short — long answers are cut at a sentence boundary with a pointer to the screen.
The displayed text is never changed; this only shapes what is spoken.
"""
from __future__ import annotations

import re

MAX_SPOKEN_CHARS = 600

_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF‍️←-⇿⌀-⏿⬀-⯿]"
)
_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_JSON_BLOB = re.compile(r"[\{\[][^{}\[\]]*[:,][^{}\[\]]*[\}\]]")


def _domain_of(url: str) -> str:
    m = re.match(r"(?:https?://)?(?:www\.)?([^/\s?#]+)", url, re.I)
    return m.group(1) if m else "the link"


def to_speech(text: str, limit: int = MAX_SPOKEN_CHARS) -> str:
    if not text:
        return ""
    t = _CODE_BLOCK.sub(" ", text)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = _JSON_BLOB.sub(" ", t)
    t = re.sub(r"\[([^\]]+)\]\((?:https?://)[^)]+\)", r"\1", t)  # markdown links → their label
    t = _URL.sub(lambda m: _domain_of(m.group(0)), t)
    t = re.sub(r"(\*\*|__|\*|_|~~|#+\s)", "", t)
    t = _EMOJI.sub("", t)

    out_lines: list[str] = []
    for raw in t.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)[.)]\s+(.*)$", line)
        if m:
            line = f"Option {m.group(1)}: {m.group(2)}"
        line = re.sub(r"^[•\-–—*·▪►✓✔]+\s*", "", line)
        if line and line[-1] not in ".!?:;":
            line += "."
        out_lines.append(line)
    t = " ".join(out_lines)

    t = re.sub(r"[“”\"«»]", "", t)
    t = re.sub(r"\s*[—–]\s*", ", ", t)  # dashes read better as a short pause
    t = re.sub(r"\(([^)]*)\)", r", \1,", t)
    t = re.sub(r"[|<>{}\[\]\\^=+#]", " ", t)
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)
    t = re.sub(r"([,.!?;:])\1+", r"\1", t)
    t = re.sub(r",\s*([.!?])", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,")

    if len(t) > limit:
        cut = t[:limit]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        t = (cut[: end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0] + ".") + " The rest is on your screen."
    return t
