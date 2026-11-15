"""Pydantic schemas: strict validation for every REST and tool input."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from .timeparse import is_valid_timezone

Priority = Literal["low", "medium", "high", "urgent"]
TaskStatus = Literal["pending", "in_progress", "completed", "cancelled"]
ReminderStatus = Literal["pending", "snoozed", "fired", "completed", "cancelled"]
ReminderKind = Literal["reminder", "alarm", "meeting"]
Recurrence = Literal["daily", "weekdays", "weekly", "monthly"]
FileStatus = Literal["saved", "requested", "in_progress", "complete", "failed", "interrupted"]

MAX_URL = 2048
_FOLDER_RE = re.compile(r"^[A-Za-z0-9 _\-]{1,40}(/[A-Za-z0-9 _\-]{1,40}){0,2}$")


def validate_http_url(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    value = value.strip()
    if len(value) > MAX_URL:
        raise ValueError("URL too long")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("only http(s) URLs are allowed")
    return value


def sanitize_folder(value: str | None) -> str | None:
    """Download sub-folders are relative to the browser's Downloads dir; no traversal allowed."""
    if value is None:
        return None
    value = value.strip().strip("/").replace("\\", "/")
    if not value:
        return None
    if ".." in value or not _FOLDER_RE.match(value):
        raise ValueError("folder may contain letters, numbers, spaces, - and _ (max 3 levels)")
    return value


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return (name or "download")[:200]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- Users / auth ----------
class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    timezone: str = "UTC"
    device_name: str = Field(default="chrome-extension", max_length=80)
    registration_secret: str | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        if not is_valid_timezone(v):
            raise ValueError("unknown IANA timezone")
        return v


class UserOut(ORM):
    id: int
    name: str
    email: str
    timezone: str
    preferences: dict
    created_at: datetime


class RegisterOut(BaseModel):
    user: UserOut
    token: str


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    timezone: str | None = None
    preferences: dict[str, Any] | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        if v is not None and not is_valid_timezone(v):
            raise ValueError("unknown IANA timezone")
        return v


# ---------- Websites ----------
class WebsiteIn(BaseModel):
    url: str
    title: str = Field(default="", max_length=500)
    notes: str | None = Field(default=None, max_length=5000)
    category: str | None = Field(default=None, max_length=60)

    _v_url = field_validator("url")(lambda cls, v: validate_http_url(v))


class WebsiteUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=5000)
    category: str | None = Field(default=None, max_length=60)


class WebsiteOut(ORM):
    id: int
    url: str
    title: str
    domain: str
    notes: str | None
    category: str | None
    visit_count: int
    created_at: datetime
    last_visited_at: datetime


class VisitIn(BaseModel):
    url: str
    title: str | None = Field(default=None, max_length=500)
    _v_url = field_validator("url")(lambda cls, v: validate_http_url(v))


# ---------- Reminders ----------
class ReminderIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    kind: ReminderKind = "reminder"
    scheduled_at: datetime
    timezone: str | None = None
    recurrence: Recurrence | None = None
    notify_offsets: list[int] = Field(default_factory=lambda: [0])
    related_url: str | None = None
    related_title: str | None = Field(default=None, max_length=500)
    task_id: int | None = None

    _v_url = field_validator("related_url")(lambda cls, v: validate_http_url(v))

    @field_validator("scheduled_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("scheduled_at must include a timezone offset")
        return v

    @field_validator("notify_offsets")
    @classmethod
    def _offsets(cls, v: list[int]) -> list[int]:
        if not v or len(v) > 5 or any(o < 0 or o > 7 * 24 * 60 for o in v):
            raise ValueError("1-5 offsets, each 0..10080 minutes")
        return sorted(set(v), reverse=True)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v):
        if v is not None and not is_valid_timezone(v):
            raise ValueError("unknown IANA timezone")
        return v


class ReminderUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    scheduled_at: datetime | None = None
    recurrence: Recurrence | None = None
    notify_offsets: list[int] | None = None
    status: Literal["pending", "completed", "cancelled"] | None = None

    @field_validator("scheduled_at")
    @classmethod
    def _aware(cls, v):
        if v is not None and v.tzinfo is None:
            raise ValueError("scheduled_at must include a timezone offset")
        return v


class SnoozeIn(BaseModel):
    minutes: int = Field(default=10, ge=1, le=7 * 24 * 60)


class ReminderOut(ORM):
    id: int
    title: str
    description: str | None
    kind: str
    scheduled_at: datetime
    next_fire_at: datetime
    timezone: str
    recurrence: str | None
    notify_offsets: list[int]
    status: str
    snoozed_until: datetime | None
    related_url: str | None
    related_title: str | None
    task_id: int | None
    last_fired_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


# ---------- Tasks ----------
class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    due_at: datetime | None = None
    due_has_time: bool = False
    priority: Priority = "medium"
    recurrence: Recurrence | None = None
    related_url: str | None = None
    related_title: str | None = Field(default=None, max_length=500)

    _v_url = field_validator("related_url")(lambda cls, v: validate_http_url(v))

    @field_validator("due_at")
    @classmethod
    def _aware(cls, v):
        if v is not None and v.tzinfo is None:
            raise ValueError("due_at must include a timezone offset")
        return v


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    due_at: datetime | None = None
    due_has_time: bool | None = None
    priority: Priority | None = None
    status: TaskStatus | None = None
    recurrence: Recurrence | None = None

    @field_validator("due_at")
    @classmethod
    def _aware(cls, v):
        if v is not None and v.tzinfo is None:
            raise ValueError("due_at must include a timezone offset")
        return v


class TaskOut(ORM):
    id: int
    title: str
    description: str | None
    due_at: datetime | None
    due_has_time: bool
    priority: str
    status: str
    recurrence: str | None
    related_url: str | None
    related_title: str | None
    completed_at: datetime | None
    created_at: datetime


# ---------- Files ----------
class FileIn(BaseModel):
    file_url: str
    file_name: str | None = Field(default=None, max_length=255)
    file_type: str | None = Field(default=None, max_length=100)
    size_bytes: int | None = Field(default=None, ge=0)
    folder: str | None = None
    notes: str | None = Field(default=None, max_length=5000)
    source_page_url: str | None = None
    source_page_title: str | None = Field(default=None, max_length=500)
    download: bool = True

    _v_url = field_validator("file_url")(lambda cls, v: validate_http_url(v))
    _v_src = field_validator("source_page_url")(lambda cls, v: validate_http_url(v))
    _v_folder = field_validator("folder")(lambda cls, v: sanitize_folder(v))


class FileStatusUpdate(BaseModel):
    status: FileStatus
    local_download_id: int | None = None
    local_path: str | None = Field(default=None, max_length=1000)
    size_bytes: int | None = Field(default=None, ge=0)
    file_name: str | None = Field(default=None, max_length=255)
    error: str | None = Field(default=None, max_length=200)


class FileUpdate(BaseModel):
    notes: str | None = Field(default=None, max_length=5000)
    folder: str | None = None
    _v_folder = field_validator("folder")(lambda cls, v: sanitize_folder(v))


class FileOut(ORM):
    id: int
    file_name: str
    file_url: str
    file_type: str | None
    size_bytes: int | None
    folder: str | None
    local_download_id: int | None
    local_path: str | None
    status: str
    error: str | None
    source_page_url: str | None
    source_page_title: str | None
    notes: str | None
    created_at: datetime


# ---------- Assistant ----------
class LinkCandidate(BaseModel):
    url: str
    text: str = Field(default="", max_length=200)
    ext: str | None = Field(default=None, max_length=10)
    size: int | None = Field(default=None, ge=0)
    _v_url = field_validator("url")(lambda cls, v: validate_http_url(v))


class PageContext(BaseModel):
    """Minimal browser context. `text` is only present when the user allowed page reading."""

    url: str | None = None
    title: str | None = Field(default=None, max_length=500)
    content_type: str | None = Field(default=None, max_length=100)
    selection: str | None = Field(default=None, max_length=4000)
    text: str | None = Field(default=None, max_length=20000)
    description: str | None = Field(default=None, max_length=500)
    file_links: list[LinkCandidate] = Field(default_factory=list, max_length=25)
    restricted: bool = False  # chrome://, web store, etc.

    @field_validator("url")
    @classmethod
    def _url(cls, v):
        if v is None:
            return None
        try:
            return validate_http_url(v)
        except ValueError:
            return None  # non-http pages are treated as "no page"


class AssistantIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    message: str = Field(min_length=1, max_length=2000)
    page: PageContext | None = None
    timezone: str | None = None
    # Client can mark that page text was provided after explicit user permission.
    page_text_allowed: bool = False
    # "voice" when the message was spoken: replies are kept short and easy to listen to.
    channel: Literal["text", "voice"] = "text"

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v):
        if v is not None and not is_valid_timezone(v):
            return None
        return v


class ClientAction(BaseModel):
    type: Literal[
        "open_url", "download", "sync_reminders", "request_page_content", "show_view", "speak",
        "connect_calendar",  # show a "Connect Google Calendar" button (OAuth starts only on click)
        "show_link",  # a link button, e.g. the created Calendar event
    ]
    payload: dict[str, Any] = Field(default_factory=dict)


class TraceStep(BaseModel):
    node: str
    detail: str = ""
    ok: bool = True


class AssistantOut(BaseModel):
    reply: str
    status: Literal["done", "needs_confirmation", "needs_clarification", "needs_permission", "needs_authorization", "error"]
    intent: str
    agent: str
    client_actions: list[ClientAction] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    trace: list[TraceStep] = Field(default_factory=list)
    provider: str | None = None
    suggestions: list[str] = Field(default_factory=list)


class SuggestIn(BaseModel):
    page: PageContext


class SuggestOut(BaseModel):
    page_type: str
    message: str | None
    actions: list[dict[str, str]] = Field(default_factory=list)
