"""Conversational Agent (response side): help + open-ended conversation."""
from __future__ import annotations

from ..llm.providers import AllProvidersFailed, ChatMessage
from .base import AgentInput, result

HELP = """Here's what I can do:
• Reminders & alarms — "Remind me to watch this video tomorrow at 8 PM", "Set an alarm for my meeting at 3 PM", "Cancel my 5 PM reminder"
• Tasks — "Create a task to prepare for my interview next Friday", "Show my tasks for today", "Mark the interview task as done"
• Website memory — "Remember this website", "Find the website about LangGraph", "Open the website I saved yesterday"
• Files — "Save this PDF", "Download this file to my project folder", "Find the document I downloaded yesterday"
• Page help — "What is this website about?", "Summarize this page\""""

PERSONA = """You are NOVA, a capable, friendly personal assistant that lives in the user's Chrome browser.
What you can actually do (through other parts of NOVA, not in this reply): reminders and alarms, tasks, saving and
reopening websites, downloading files from the page, summarising the current page, and scheduling events on Google
Calendar once the user connects it.

How to answer:
- Work out what the user actually wants and answer that directly. Lead with the answer, no preamble.
- Match the length to the question: a quick question gets one or two sentences; "explain", "how does", "compare" or
  "help me understand" gets a clear, well-structured explanation (short paragraphs or a few bullets).
- In THIS reply you are only talking. Never claim you did something (set, saved, opened, scheduled). If they seem to
  want an action NOVA can do, say so and give the exact phrase, e.g. Say "remind me at 6 PM to call Riya".
- If the request is ambiguous or missing something essential, ask one short clarifying question instead of guessing.
- Be accurate. If you aren't sure or it depends on recent information you may not have, say so briefly.
- Use the conversation so far for context (e.g. "what about tomorrow?" continues the previous topic).
"""

VOICE_STYLE = """The user is TALKING to you and will HEAR your reply read aloud. Reply in natural spoken English:
plain sentences, no markdown, no bullet points, no emoji, no URLs or code. Keep it under about 60 words unless they asked
for an explanation, and then still keep it to the essentials (under about 120 words). Spell out symbols ("percent")."""


GREETING_REPLIES = {
    "thanks": "You're welcome! Anything else I can take care of?",
    "hello": "Hi! I'm NOVA. Ask me to remind you about something, save this page, or show your tasks.",
}


def run(inp: AgentInput) -> dict:
    if inp.intent.intent == "help":
        return result(HELP)
    router = inp.deps.router
    if inp.intent.intent == "greeting" and (not router or not router.any_configured()):
        thanks = any(w in inp.message.lower() for w in ("thank", "thx", "cheers"))
        return result(GREETING_REPLIES["thanks" if thanks else "hello"])
    if not router or not router.any_configured():
        return result(
            "I can't answer open-ended questions right now because no AI provider is configured on the server — "
            "but I can still handle reminders, tasks, website memory and downloads.\n\n" + HELP,
            status="error",
        )
    page = inp.page
    ctx = f"(User is on: {page.get('title')} — {page.get('domain')})" if page.get("url") else "(No page context)"
    voice = inp.extra.get("channel") == "voice"
    msgs = [ChatMessage("system", PERSONA + (VOICE_STYLE if voice else ""))]
    for h in inp.extra.get("history", [])[-6:]:
        msgs.append(ChatMessage("user" if h["role"] == "user" else "assistant", h["content"][:800]))
    msgs.append(ChatMessage("user", f"{ctx}\n{inp.message}"))
    try:
        res = router.complete(msgs, temperature=0.5, preferred=inp.deps.preferred, deadline_s=router.settings.chat_deadline_seconds)
        return result(res.text.strip(), data={"provider": res.provider})
    except AllProvidersFailed as exc:
        return result(f"{exc.user_message} I can still handle reminders, tasks, saved websites and downloads.", status="error")
