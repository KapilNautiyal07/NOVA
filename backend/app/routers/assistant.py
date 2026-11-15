from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..agents.context import build_page_profile, suggestion_for
from ..agents.runner import get_conversation, handle_message
from ..config import get_settings
from ..database import get_db
from ..llm.providers import get_router
from ..voice import service as voice
from ..voice.gemini import VOICES
from ..models import Conversation, Message, Reminder, SavedFile, SavedWebsite, Task, User
from ..schemas import AssistantIn, AssistantOut, FileOut, ReminderOut, SuggestIn, SuggestOut, TaskOut, UserOut, WebsiteOut
from ..security import current_user, rate_limited

router = APIRouter(prefix="/api", tags=["assistant"])


@router.post("/assistant/message", response_model=AssistantOut)
def assistant_message(body: AssistantIn, user: User = Depends(rate_limited), db: Session = Depends(get_db)):
    """One conversational turn through the LangGraph agent."""
    return handle_message(db, user, body, get_router())


@router.post("/assistant/suggest", response_model=SuggestOut)
def suggest(body: SuggestIn, user: User = Depends(current_user)):
    """Contextual suggestion from URL/title only — no page text, no AI call."""
    profile = build_page_profile(body.page, allow_text=False, max_chars=0)
    msg, actions = suggestion_for(profile["page_type"])
    return SuggestOut(page_type=profile["page_type"], message=msg, actions=actions)


@router.get("/conversations/{session_id}/messages")
def conversation_messages(session_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    conv = get_conversation(db, user, session_id[:64])
    msgs = db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.id.desc()).limit(50))
    return [{"role": m.role, "content": m.content, "created_at": m.created_at.isoformat(), "meta": m.meta} for m in reversed(list(msgs))]


@router.post("/voice/transcribe")
async def transcribe(audio: UploadFile = File(...), preview: bool = Form(False), user: User = Depends(rate_limited)):
    """Speech-to-text: Gemini transcription (smart mode, en-IN/en-US hints), Groq Whisper fallback.
    `preview=true` marks a live caption taken while the user is still speaking."""
    settings = get_settings()
    data = await audio.read()
    if not data:
        raise HTTPException(400, "Empty audio")
    if len(data) > settings.max_audio_bytes:
        raise HTTPException(413, "Audio too long")
    mime = (audio.content_type or "audio/webm").split(";")[0]
    if not mime.startswith("audio/"):
        raise HTTPException(415, "Unsupported audio type")
    try:
        t = await run_in_threadpool(lambda: voice.transcribe(data, audio.filename or "speech.webm", mime, preview=preview))
    except voice.VoiceUnavailable as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return {"text": t.text, "provider": t.provider, "fallback_from": t.fallback_from}


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: str | None = Field(default=None, max_length=40)


@router.post("/voice/speak")
async def speak(body: SpeakIn, user: User = Depends(rate_limited)):
    """Text-to-speech with Gemini TTS. Returns base64 WAV plus the exact text that was spoken
    (markdown, links and emoji are removed, long answers shortened)."""
    try:
        audio, spoken = await run_in_threadpool(voice.synthesize, body.text, voice=body.voice)
    except voice.VoiceUnavailable as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return {"audio": base64.b64encode(audio).decode(), "mime": "audio/wav", "spoken_text": spoken}


@router.get("/voice/config")
def voice_config(user: User = Depends(current_user)):
    settings = get_settings()
    g = voice.gemini_voice()
    groq = get_router().providers.get("groq")
    return {
        "stt": [p for p in settings.stt_providers.split(",") if (p == "gemini" and g.configured()) or (p == "groq" and groq and groq.configured())],
        "tts": g.configured(),
        "voices": VOICES,
        "default_voice": settings.tts_default_voice,
    }


@router.get("/data/export")
def export_data(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Everything NOVA stores about the user, as JSON."""
    def rows(model, schema):
        return [schema.model_validate(r).model_dump(mode="json") for r in db.scalars(select(model).where(model.user_id == user.id))]

    return {
        "user": UserOut.model_validate(user).model_dump(mode="json"),
        "reminders": rows(Reminder, ReminderOut),
        "tasks": rows(Task, TaskOut),
        "websites": rows(SavedWebsite, WebsiteOut),
        "files": rows(SavedFile, FileOut),
        "conversations": [
            {"session_id": c.session_id, "messages": [{"role": m.role, "content": m.content, "at": m.created_at.isoformat()} for m in c.messages]}
            for c in db.scalars(select(Conversation).where(Conversation.user_id == user.id))
        ],
    }


@router.delete("/data")
def delete_data(confirm: str, delete_account: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Delete all stored data. Requires ?confirm=DELETE."""
    if confirm != "DELETE":
        raise HTTPException(400, "Pass confirm=DELETE to erase your data")
    for model in (Reminder, Task, SavedWebsite, SavedFile, Conversation):
        db.execute(delete(model).where(model.user_id == user.id))
    if delete_account:
        db.delete(user)
    db.commit()
    return {"deleted": True, "account_deleted": delete_account}


@router.get("/health/providers")
def provider_health(user: User = Depends(current_user)):
    return get_router().status()
