"""Website Memory Agent: save / search / reopen websites the user chose to remember."""
from __future__ import annotations

import re

from ..timeparse import humanize, parse_period
from .base import AgentInput, quote, result, ser_site

RESTRICTED = "I can't access this page — browser-internal pages (like chrome:// or the Web Store) are off-limits to extensions."


def _needs_page(inp: AgentInput) -> dict | None:
    if not inp.page.get("url"):
        return result(RESTRICTED if inp.page.get("restricted") else "Open the page you'd like me to save, then ask again.",
                      status="error")
    return None


def save(inp: AgentInput) -> dict:
    if (err := _needs_page(inp)):
        return err
    page = inp.page
    site, created = inp.tools.call("save_website", url=page["url"], title=page.get("title") or "", notes=inp.intent.notes)
    reply = (f"Saved {quote(site.title)} to your website memory." if created
             else f"{quote(site.title)} was already in your memory — I've updated it.")
    return result(reply, data={"website": ser_site(site)},
                  suggestions=["Remind me to visit this website tomorrow at 10 AM"])


def save_note(inp: AgentInput) -> dict:
    if (err := _needs_page(inp)):
        return err
    page = inp.page
    note = inp.intent.notes or page.get("selection")
    if not note:
        return result("Select the text you want to keep (or say \"note that …\"), then ask me again.", status="needs_clarification")
    site, _ = inp.tools.call("save_website", url=page["url"], title=page.get("title") or "", notes=note[:5000])
    preview = note.strip().replace("\n", " ")
    preview = preview[:80] + ("…" if len(preview) > 80 else "")
    return result(f"Saved for later from {quote(site.title, 50)}: “{preview}”", data={"website": ser_site(site)})


def _search(inp: AgentInput):
    it = inp.intent
    now = inp.deps.now
    period = parse_period(it.period or "", now) if it.period else parse_period(inp.message, now)
    start, end, label = period if period else (None, None, None)
    sites = inp.tools.call("search_websites", query=it.query or "", start=start, end=end, limit=20)
    widened = False
    if not sites and period and it.query:
        sites = inp.tools.call("search_websites", query=it.query, limit=20)
        widened = bool(sites)
    return sites, label, widened


def search(inp: AgentInput) -> dict:
    sites, label, widened = _search(inp)
    q = inp.intent.query
    scope = (f" about {quote(q)}" if q else "") + (f" from {label}" if label and not widened else "")
    if not sites:
        return result(f"I couldn't find any saved websites{scope}.", data={"websites": []},
                      actions=[{"type": "show_view", "payload": {"view": "memory"}}])
    now = inp.deps.now
    lines = [f"• {s.title} ({s.domain}) — saved {humanize(s.created_at, now).split(' at ')[0]}" for s in sites[:5]]
    note = f" (nothing from {label}, but here's what matches)" if widened else ""
    more = f"\n…and {len(sites) - 5} more in your memory." if len(sites) > 5 else ""
    return result(f"I found {len(sites)} saved website{'s' if len(sites) != 1 else ''}{scope}{note}:\n" + "\n".join(lines) + more,
                  data={"websites": [ser_site(s) for s in sites], "query": q},
                  actions=[{"type": "show_view", "payload": {"view": "memory"}}])


KNOWN_SITES = {
    "youtube": "https://www.youtube.com", "gmail": "https://mail.google.com", "google": "https://www.google.com",
    "google calendar": "https://calendar.google.com", "calendar": "https://calendar.google.com",
    "google drive": "https://drive.google.com", "drive": "https://drive.google.com", "google docs": "https://docs.google.com",
    "google maps": "https://maps.google.com", "maps": "https://maps.google.com", "github": "https://github.com",
    "linkedin": "https://www.linkedin.com", "whatsapp": "https://web.whatsapp.com", "whatsapp web": "https://web.whatsapp.com",
    "twitter": "https://x.com", "x": "https://x.com", "facebook": "https://www.facebook.com", "instagram": "https://www.instagram.com",
    "reddit": "https://www.reddit.com", "wikipedia": "https://www.wikipedia.org", "amazon": "https://www.amazon.in",
    "flipkart": "https://www.flipkart.com", "netflix": "https://www.netflix.com", "spotify": "https://open.spotify.com",
    "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai", "stack overflow": "https://stackoverflow.com",
    "stackoverflow": "https://stackoverflow.com", "notion": "https://www.notion.so", "outlook": "https://outlook.live.com",
}
SITE_NAMES = {
    "youtube": "YouTube", "github": "GitHub", "linkedin": "LinkedIn", "whatsapp": "WhatsApp", "whatsapp web": "WhatsApp Web",
    "chatgpt": "ChatGPT", "stackoverflow": "Stack Overflow", "x": "X", "google calendar": "Google Calendar",
    "calendar": "Google Calendar", "drive": "Google Drive", "maps": "Google Maps",
}
_DOMAIN = re.compile(r"^(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})(/\S*)?$", re.I)


def direct_site(query: str | None) -> tuple[str, str] | None:
    """"youtube" → YouTube's URL; "example.com" → https://example.com."""
    q = re.sub(r"\b(the|website|site|page|app|web|please|for me|again|now)\b", " ", (query or "").lower())
    q = re.sub(r"\s+", " ", q).strip(" .!?")
    if not q:
        return None
    if q in KNOWN_SITES:
        return KNOWN_SITES[q], SITE_NAMES.get(q, q.title() if len(q) > 3 else q.upper())
    m = _DOMAIN.match(q.replace(" dot ", "."))
    if m:
        return f"https://{m.group(1)}{m.group(2) or ''}", m.group(1)
    return None


def open_(inp: AgentInput) -> dict:
    if url := inp.extra.get("direct_url"):  # follow-up: "open that website again"
        title = inp.extra.get("direct_title") or url
        return result(f"Opening {quote(title)} again.", actions=[{"type": "open_url", "payload": {"url": url}}],
                      data={"last_action": {"intent": "open_website", "title": title, "website_url": url, "website_title": title}})
    sites, label, widened = _search(inp)
    if not sites and not label and (site := direct_site(inp.intent.query)):
        url, name = site
        return result(f"Opening {name}.", actions=[{"type": "open_url", "payload": {"url": url}}],
                      data={"last_action": {"intent": "open_website", "title": name, "website_url": url, "website_title": name}})
    if not sites:
        scope = f" from {label}" if label else ""
        what = f" matching {quote(inp.intent.query)}" if inp.intent.query else ""
        return result(f"I couldn't find a saved website{what}{scope}. I only search websites you've saved with NOVA.",
                      data={"websites": []})
    best = sites[0]
    reply = f"Opening {quote(best.title)} ({best.domain})."
    if len(sites) > 1:
        reply += f" I found {len(sites) - 1} other match{'es' if len(sites) > 2 else ''} too — they're in your website memory."
    return result(reply, actions=[{"type": "open_url", "payload": {"url": best.url}}],
                  data={"website": ser_site(best), "websites": [ser_site(s) for s in sites[:5]]})


def run(inp: AgentInput) -> dict:
    return {"save_website": save, "save_note": save_note, "search_websites": search, "open_website": open_}[inp.intent.intent](inp)
