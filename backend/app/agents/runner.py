"""Runs one assistant turn: loads conversation state, invokes the graph, persists results."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..llm.providers import LLMRouter
from ..models import Conversation, Message, User
from ..schemas import AssistantIn, AssistantOut
from ..timeparse import get_tz
from . import followups
from .graph import GRAPH
from .tools import Deps
from .utterance import normalize


def get_conversation(db: Session, user: User, session_id: str) -> Conversation:
    conv = db.scalar(select(Conversation).where(Conversation.user_id == user.id, Conversation.session_id == session_id))
    if not conv:
        conv = Conversation(user_id=user.id, session_id=session_id, state={})
        db.add(conv)
        db.commit()
    return conv


def handle_message(
    db: Session, user: User, payload: AssistantIn, router: LLMRouter | None, now: datetime | None = None
) -> AssistantOut:
    settings = get_settings()
    if payload.timezone and payload.timezone != user.timezone:
        user.timezone = payload.timezone  # keep scheduling correct when the user travels
        db.commit()
    tz = get_tz(user.timezone)
    now_local = (now or datetime.now(tz)).astimezone(tz)

    conv = get_conversation(db, user, payload.session_id)
    recent = list(db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.id.desc()).limit(8)))
    history = [{"role": m.role, "content": m.content} for m in reversed(recent)]

    deps = Deps(db=db, user=user, router=router, settings=settings, now=now_local)
    state = GRAPH.invoke(
        {
            "message": normalize(payload.message),
            "raw_page": payload.page,
            "page_text_allowed": payload.page_text_allowed,
            "history": history,
            "pending": (conv.state or {}).get("pending"),
            "last": (conv.state or {}).get("last"),
            "channel": payload.channel,
            "trace": [],
        },
        config={"configurable": {"deps": deps}},
    )
    out = state["output"]
    intent = (state.get("intent") or {}).get("intent", "general")
    agent = state.get("route") if state.get("route") not in (None, "respond", "classify") else "chat"
    provider = state.get("provider") or out.get("data", {}).pop("provider", None)

    prev_last = (conv.state or {}).get("last")
    last = followups.last_from(intent, out.get("data") or {}, prev_last) if out["status"] != "error" else None
    new_state: dict = {"last": last or prev_last} if (last or prev_last) else {}
    if out.get("pending"):
        new_state["pending"] = out["pending"]
    conv.state = new_state
    out.get("data", {}).pop("last_action", None)
    db.add(Message(conversation_id=conv.id, role="user", content=payload.message.strip()[:2000],
                   meta={"page_url": (state.get("page") or {}).get("url")}))
    db.add(Message(conversation_id=conv.id, role="assistant", content=out["reply"][:8000],
                   meta={"intent": intent, "status": out["status"], "provider": provider}))
    db.commit()

    return AssistantOut(
        reply=out["reply"],
        status=out["status"],
        intent=intent,
        agent=agent,
        client_actions=out.get("client_actions", []),
        data=out.get("data", {}),
        trace=state.get("trace", []),
        provider=provider,
        suggestions=out.get("suggestions", []),
    )
