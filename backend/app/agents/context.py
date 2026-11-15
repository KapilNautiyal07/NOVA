"""Browser Context Agent: turns raw tab info into a minimal, sanitised page profile.

It decides *what kind of page* the user is on (used for routing + contextual suggestions)
without reading page content. Page text is only ever passed through when the user allowed it.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from ..schemas import PageContext
from ..services.common import domain_of
from ..services.files import looks_like_file_url

JOB_HINTS = ("linkedin.com/jobs", "indeed.", "glassdoor.", "greenhouse.io", "lever.co", "myworkdayjobs", "naukri.com", "wellfound.com", "/careers", "/jobs/")
DOC_HINTS = ("docs.", "readthedocs", "/docs", "/documentation", "developer.", "devdocs", "/reference", "/api/", "/guide", "learn.microsoft.com", "developer.mozilla.org")


def detect_page_type(url: str | None, title: str | None = None, content_type: str | None = None) -> str:
    if not url:
        return "none"
    low = url.lower()
    host = domain_of(url)
    path = urlparse(url).path
    t = (title or "").lower()
    if (content_type or "").startswith("application/pdf") or low.split("?")[0].endswith(".pdf"):
        return "pdf"
    if host in ("youtube.com", "m.youtube.com") and (path == "/watch" or path.startswith("/shorts/")) or host == "youtu.be":
        return "video"
    if host == "github.com":
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] not in ("orgs", "settings", "notifications", "marketplace", "topics", "search"):
            return "github_repo"
        return "code"
    if any(h in low for h in JOB_HINTS) or re.search(r"\b(job|hiring|careers?)\b", t) and re.search(r"\b(apply|engineer|developer|manager|intern)\b", t):
        return "job_listing"
    if any(h in low for h in DOC_HINTS) or "documentation" in t:
        return "documentation"
    if host.startswith("google.") or host in ("bing.com", "duckduckgo.com") and "q" in parse_qs(urlparse(url).query):
        return "search"
    if looks_like_file_url(url, content_type if content_type and content_type != "text/html" else None):
        return "file"
    if re.search(r"/(blog|article|posts?|news|story)/", low) or host in ("medium.com", "dev.to", "substack.com"):
        return "article"
    return "webpage"


def build_page_profile(page: PageContext | None, allow_text: bool, max_chars: int) -> dict:
    if page is None or page.restricted or not page.url:
        return {"url": None, "restricted": bool(page and page.restricted), "page_type": "none"}
    page_type = detect_page_type(page.url, page.title, page.content_type)
    profile = {
        "url": page.url,
        "title": (page.title or "").strip()[:500] or domain_of(page.url),
        "domain": domain_of(page.url),
        "page_type": page_type,
        "content_type": page.content_type,
        "description": (page.description or "")[:500] or None,
        "selection": (page.selection or "")[:4000] or None,
        "file_links": [l.model_dump() for l in page.file_links[:25]],
        "restricted": False,
        "text": None,
    }
    if allow_text and page.text:
        profile["text"] = sanitize_page_text(page.text, max_chars)
    return profile


def sanitize_page_text(text: str, max_chars: int) -> str:
    text = re.sub(r"[​-‏ -‮﻿]", "", text)  # zero-width / bidi tricks
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_chars]


SUGGESTIONS = {
    "github_repo": ("You're viewing a GitHub repository. Want me to explain what it does or save it?",
                    [("Explain this repo", "What is this repository about?"), ("Save repo", "Remember this website")]),
    "documentation": ("Reading documentation? I can summarize it or remind you to come back.",
                      [("Summarize", "Summarize this page"), ("Revisit tomorrow", "Remind me to read this tomorrow at 10 AM")]),
    "pdf": ("I can summarize this document or save it for you.",
            [("Save PDF", "Save this PDF"), ("Remember it", "Remember this document")]),
    "job_listing": ("Looks like a job listing. Save it and set an application reminder?",
                    [("Save job", "Remember this website"), ("Apply reminder", "Remind me to apply for this job tomorrow at 10 AM")]),
    "video": ("Want me to remind you to watch this later?",
              [("Watch tonight", "Remind me to watch this video tonight at 8 PM"), ("Save video", "Remember this website")]),
    "article": ("Want a quick summary of this article, or save it for later?",
                [("Summarize", "Summarize this page"), ("Save", "Remember this website")]),
}


def suggestion_for(page_type: str) -> tuple[str | None, list[dict[str, str]]]:
    if page_type not in SUGGESTIONS:
        return None, []
    msg, actions = SUGGESTIONS[page_type]
    return msg, [{"label": label, "command": cmd} for label, cmd in actions]
