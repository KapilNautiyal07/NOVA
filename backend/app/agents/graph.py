"""LangGraph orchestration of NOVA's agents.

    START → browser_context ─┬─(pending step?)→ resume ─┬→ <specialist agent> → respond → END
                             │                          ├→ classify
                             │                          └→ respond   (user declined)
                             └────────────────────────→ classify → <specialist agent> → respond → END

Specialists: reminder | task | memory | file | page | chat. Each one only calls tools from
the validated ToolBox. Sensitive actions return `needs_confirmation`, which is stored as a
pending step and resumed on the next turn ("yes"/"no").
"""
from __future__ import annotations

import logging
import operator
import re
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from ..services.common import NotFound
from ..timeparse import parse_when
from . import calendar_agent, chat_agent, file_agent, followups, memory_agent, page_agent, reminder_agent, task_agent
from .base import NO, YES, AgentInput, resolve_choice, result
from .context import build_page_profile
from .intents import INTENT_AGENT, IntentResult, classify, rule_classify
from .tools import Deps, ToolBox

log = logging.getLogger("nova.graph")

AGENTS = {
    "reminder": reminder_agent.run,
    "task": task_agent.run,
    "memory": memory_agent.run,
    "file": file_agent.run,
    "page": page_agent.run,
    "chat": chat_agent.run,
    "calendar": calendar_agent.run,
}


class GraphState(TypedDict, total=False):
    message: str
    raw_page: Any  # PageContext | None
    page_text_allowed: bool
    page: dict
    history: list[dict]
    pending: dict | None
    intent: dict
    confirmed: bool
    extra: dict
    route: str
    output: dict
    provider: str | None
    nlu_note: str | None
    last: dict | None  # what NOVA did last turn (for "do that tomorrow")
    channel: str  # "text" | "voice"
    trace: Annotated[list[dict], operator.add]


def _deps(config: RunnableConfig) -> Deps:
    return config["configurable"]["deps"]


# ----------------------------------------------------------------- nodes
def browser_context(state: GraphState, config: RunnableConfig) -> dict:
    deps = _deps(config)
    profile = build_page_profile(state.get("raw_page"), state.get("page_text_allowed", False), deps.settings.max_page_chars)
    detail = f"{profile.get('page_type')} · {profile.get('domain')}" if profile.get("url") else ("restricted page" if profile.get("restricted") else "no page")
    return {"page": profile, "trace": [{"node": "browser_context", "detail": detail, "ok": True}]}


def _starts_new_request(msg: str, pending_intent: IntentResult, has_page: bool) -> bool:
    """True when the reply is itself a complete new command rather than an answer.

    Without this, a pending "what time?" question would swallow e.g. "Remind me at 5 PM to
    stretch" and graft its time onto the old request.
    """
    rc = rule_classify(msg, has_page)
    if rc.intent in ("general", "help", "greeting"):
        return False
    # "Remind me tomorrow at 10" in answer to "When should I remind you?" is still an answer.
    if rc.intent == pending_intent.intent and not rc.title:
        return False
    return True


def resume(state: GraphState, config: RunnableConfig) -> dict:
    """Continue a pending clarification/confirmation, or drop it if the user moved on."""
    pending = state["pending"] or {}
    msg = state["message"].strip()
    ptype = pending.get("type")
    intent = IntentResult.model_validate(pending.get("intent") or {"intent": "general"})
    extra = dict(pending.get("extra") or {})
    agent = INTENT_AGENT[intent.intent]
    trace = [{"node": "resume", "detail": f"pending {ptype}", "ok": True}]
    if intent.intent == "schedule_event" or ptype == "auth":
        # The calendar agent runs its own slot-filling/confirmation dialogue.
        if calendar_agent.absorbs(msg, pending, _deps(config).now):
            return {"intent": intent.model_dump(), "route": "calendar", "confirmed": False, "trace": trace,
                    "extra": {**extra, "calendar_reply": (ptype, pending.get("missing"))}}
        trace[0]["detail"] += " → dropped (new request)"
        return {"route": "classify", "pending": None, "trace": trace}
    if ptype in ("slot", "choose") and _starts_new_request(msg, intent, bool((state.get("page") or {}).get("url"))):
        trace[0]["detail"] += " → dropped (new request)"
        return {"route": "classify", "pending": None, "trace": trace}

    def go(**changes) -> dict:
        upd = intent.model_copy(update=changes.pop("intent_update", {}))
        return {"intent": upd.model_dump(), "extra": {**extra, **changes.pop("extra", {})}, "route": agent,
                "confirmed": changes.pop("confirmed", False), "trace": trace}

    def cancelled() -> dict:
        return {"route": "respond", "output": result("Okay — I won't do that."), "trace": trace}

    if ptype == "confirm":
        if YES.match(msg):
            return go(confirmed=True)
        if NO.match(msg):
            return cancelled()
    elif ptype == "choose":
        opt = resolve_choice(msg, pending.get("options") or [])
        if opt:
            ch = {"chosen_id": opt["id"]}
            if "url" in opt:
                ch.update(chosen_url=opt["url"], chosen_label=opt["label"])
            return go(extra=ch)
        if NO.match(msg):
            return cancelled()
    elif ptype == "slot":
        missing = pending.get("missing")
        now = _deps(config).now
        if NO.match(msg) and len(msg.split()) <= 3:
            return cancelled()
        if missing == "when":
            if parse_when(msg, now):
                return go(intent_update={"when": msg, "datetime_iso": None})
        elif missing == "time":
            combined = f"{extra.get('date_text', '')} {msg}".strip()
            p = parse_when(msg, now)
            if p and p.has_date and p.has_time:  # user restated a full date+time
                return go(intent_update={"when": msg, "datetime_iso": None})
            p = parse_when(combined, now)
            if p and p.has_time:
                return go(intent_update={"when": combined, "datetime_iso": None})
            if re.fullmatch(r"\d{1,2}(:\d{2})?", msg):
                return go(intent_update={"when": f"{extra.get('date_text', '')} at {msg}", "datetime_iso": None})
        elif missing == "meridiem":
            m = re.search(r"\b(a\.?m\.?|p\.?m\.?|morning|afternoon|evening|night)\b", msg.lower())
            if m:
                word = m.group(1).replace(".", "")
                suffix = {"morning": "am", "afternoon": "pm", "evening": "pm", "night": "pm"}.get(word, word)
                base = extra.get("date_text", "")
                combined = re.sub(r"\b(at|@|by|around)\s+(\d{1,2}(?:[:.]\d{2})?)\b(?!\s*[ap]\.?m)", rf"\1 \2 {suffix}", base, count=1, flags=re.I)
                return go(intent_update={"when": combined, "datetime_iso": None}, extra={"meridiem_answer": True})
        elif missing == "title":
            if not rule_classify(msg).intent.startswith(("list_", "search_")):
                return go(intent_update={"title": msg})
        elif missing == "adjust_time":
            # NOVA defaulted a time ("I picked 9:00 AM — tell me if you'd prefer another time")
            # and the user answered with a time: move what was just created.
            cleaned = re.sub(r"^(?:no,?\s*)?(?:make it|change it to|move it to|set it (?:to|for)|actually|instead,?)\s*", "", msg, flags=re.I)
            cleaned = re.sub(r"\s*instead[.!]*$", "", cleaned, flags=re.I)
            p = parse_when(cleaned, now)
            if p and p.has_time:
                when = cleaned if p.has_date else f"{extra.get('date_text', '')} {cleaned}".strip()
                return go(intent_update={"when": when, "datetime_iso": None})
    trace[0]["detail"] += " → dropped (new request)"
    return {"route": "classify", "pending": None, "trace": trace}


def classify_node(state: GraphState, config: RunnableConfig) -> dict:
    deps = _deps(config)
    page = state.get("page") or {}
    now_text = deps.now.strftime("%A %Y-%m-%d %H:%M %Z")
    last = state.get("last")
    extra: dict = {}
    follow = followups.resolve(state["message"], last, deps.now)
    if follow:
        intent, extra = follow
        provider, note = None, None
        via = "follow-up"
    else:
        intent, provider, note = classify(deps.router, state["message"], page, state.get("history") or [], now_text,
                                          deps.preferred, followups.summarize_last(last))
        via = provider or "rules"
    agent = INTENT_AGENT[intent.intent]
    detail = f"{intent.intent} → {agent} agent via {via}"
    return {
        "intent": intent.model_dump(),
        "route": agent,
        "provider": provider,
        "nlu_note": note,
        "extra": extra,
        "confirmed": False,
        "trace": [{"node": "conversational_agent", "detail": detail, "ok": True}],
    }


def make_agent_node(name: str):
    run = AGENTS[name]

    def node(state: GraphState, config: RunnableConfig) -> dict:
        deps = _deps(config)
        intent = IntentResult.model_validate(state["intent"])
        extra = dict(state.get("extra") or {})
        # Resuming a pending step: "this website" refers to the page the request was made on.
        page = extra.pop("page", None) or state.get("page") or {}
        if name in ("chat", "page"):
            extra["history"] = state.get("history") or []
            extra["channel"] = state.get("channel") or "text"
        inp = AgentInput(
            intent=intent, message=state["message"], page=page, deps=deps,
            tools=ToolBox(deps), confirmed=state.get("confirmed", False), extra=extra,
        )
        deps.trace.clear()
        try:
            out = run(inp)
            ok = out["status"] != "error"
        except NotFound as exc:
            out, ok = result(f"I couldn't find that {exc.what} — it may have been deleted.", status="error"), False
        except (ValidationError, ValueError) as exc:
            log.info("agent %s rejected input: %s", name, exc)
            out, ok = result("I couldn't do that because some details were invalid. Could you rephrase?", status="error"), False
        except Exception:
            log.exception("agent %s crashed", name)
            out, ok = result("Something went wrong while doing that. Please try again.", status="error"), False
        steps = [{"node": f"{name}_agent", "detail": intent.intent, "ok": ok}, *deps.trace]
        return {"output": out, "trace": steps}

    node.__name__ = f"{name}_agent"
    return node


def respond(state: GraphState, config: RunnableConfig) -> dict:
    out = dict(state.get("output") or result("Sorry, I didn't catch that.", status="error"))
    return {"output": out, "trace": [{"node": "respond", "detail": out["status"], "ok": out["status"] != "error"}]}


# ----------------------------------------------------------------- graph
def build_graph():
    g = StateGraph(GraphState)
    g.add_node("browser_context", browser_context)
    g.add_node("resume", resume)
    g.add_node("classify", classify_node)
    for name in AGENTS:
        g.add_node(name, make_agent_node(name))
    g.add_node("respond", respond)

    g.add_edge(START, "browser_context")
    g.add_conditional_edges("browser_context", lambda s: "resume" if s.get("pending") else "classify",
                            {"resume": "resume", "classify": "classify"})
    g.add_conditional_edges("resume", lambda s: s["route"], {**{n: n for n in AGENTS}, "classify": "classify", "respond": "respond"})
    g.add_conditional_edges("classify", lambda s: s["route"], {n: n for n in AGENTS})
    for name in AGENTS:
        g.add_edge(name, "respond")
    g.add_edge("respond", END)
    return g.compile()


GRAPH = build_graph()
