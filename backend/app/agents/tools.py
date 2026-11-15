"""Tool layer: every side-effecting operation an agent can perform.

Each tool is a LangChain StructuredTool with a strict pydantic args schema, so every
invocation is validated before it touches the database. Agents never write to the DB directly.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..config import Settings
from ..llm.providers import LLMRouter
from ..models import User
from ..schemas import FileIn, ReminderIn, TaskIn, validate_http_url
from ..services import files as file_svc
from ..services import reminders as rem_svc
from ..services import tasks as task_svc
from ..services import websites as web_svc

log = logging.getLogger("nova.tools")


@dataclass
class Deps:
    db: Session
    user: User
    router: LLMRouter | None
    settings: Settings
    now: datetime  # aware, user's local timezone
    trace: list[dict] = field(default_factory=list)

    @property
    def preferred(self) -> str | None:
        pref = (self.user.preferences or {}).get("provider")
        return pref if pref in ("gemini", "groq") else None


class IdArgs(BaseModel):
    id: int = Field(ge=1)


class SnoozeArgs(BaseModel):
    id: int = Field(ge=1)
    minutes: int = Field(ge=1, le=10080)


class ReferenceArgs(BaseModel):
    reference: str = Field(default="", max_length=300)


def _require_aware(v: datetime | None) -> datetime | None:
    if v is not None and v.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return v


class RescheduleArgs(BaseModel):
    id: int = Field(ge=1)
    scheduled_at: datetime

    _v_aware = field_validator("scheduled_at")(lambda cls, v: _require_aware(v))


class UpdateTaskArgs(BaseModel):
    id: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    due_at: datetime | None = None
    due_has_time: bool | None = None
    priority: Literal["low", "medium", "high", "urgent"] | None = None

    _v_aware = field_validator("due_at")(lambda cls, v: _require_aware(v))


class ListRemindersArgs(BaseModel):
    statuses: list[Literal["pending", "snoozed", "fired", "completed", "cancelled"]] | None = None
    kind: Literal["reminder", "alarm", "meeting"] | None = None
    start: datetime | None = None
    end: datetime | None = None
    limit: int = Field(default=50, ge=1, le=500)


class CreateReminderArgs(ReminderIn):
    website_id: int | None = None


class ListTasksArgs(BaseModel):
    statuses: list[Literal["pending", "in_progress", "completed", "cancelled"]] | None = None
    priority: Literal["low", "medium", "high", "urgent"] | None = None
    due: Literal["today", "tomorrow", "upcoming", "overdue", "none"] | None = None
    query: str | None = Field(default=None, max_length=300)
    limit: int = Field(default=50, ge=1, le=500)


class SaveWebsiteArgs(BaseModel):
    url: str
    title: str = Field(default="", max_length=500)
    notes: str | None = Field(default=None, max_length=5000)

    _v_url = field_validator("url")(lambda cls, v: validate_http_url(v))


class SearchArgs(BaseModel):
    query: str = Field(default="", max_length=300)
    start: datetime | None = None
    end: datetime | None = None
    limit: int = Field(default=20, ge=1, le=100)


class ToolBox:
    def __init__(self, deps: Deps):
        self.deps = deps
        d, u = deps.db, deps.user
        specs: list[tuple[str, str, type[BaseModel], Any]] = [
            ("create_reminder", "Schedule a reminder/alarm/meeting notification.", CreateReminderArgs,
             lambda **a: rem_svc.create_reminder(d, u, ReminderIn(**{k: v for k, v in a.items() if k != "website_id"}), a.get("website_id"))),
            ("list_reminders", "List reminders filtered by status/kind/time window.", ListRemindersArgs,
             lambda **a: rem_svc.list_reminders(d, u, a.get("statuses"), a.get("kind"), a.get("start"), a.get("end"), a.get("limit", 50))),
            ("find_reminders", "Find active reminders matching a time or words.", ReferenceArgs,
             lambda reference: rem_svc.find_reminders(d, u, reference, deps.now)),
            ("cancel_reminder", "Cancel a reminder (destructive).", IdArgs, lambda id: rem_svc.cancel_reminder(d, u, id)),
            ("snooze_reminder", "Snooze a reminder by N minutes.", SnoozeArgs, lambda id, minutes: rem_svc.snooze_reminder(d, u, id, minutes)),
            ("complete_reminder", "Mark a reminder done.", IdArgs, lambda id: rem_svc.complete_reminder(d, u, id)),
            ("reschedule_reminder", "Move a reminder to a new time.", RescheduleArgs,
             lambda id, scheduled_at: rem_svc.update_reminder(d, u, id, scheduled_at=scheduled_at)),
            ("create_task", "Create a task.", TaskIn, lambda **a: task_svc.create_task(d, u, TaskIn(**a))),
            ("list_tasks", "List tasks with filters.", ListTasksArgs,
             lambda **a: task_svc.list_tasks(d, u, a.get("statuses"), a.get("priority"), a.get("due"), a.get("query"), a.get("limit", 50))),
            ("find_tasks", "Find open tasks matching words.", ReferenceArgs, lambda reference: task_svc.find_tasks(d, u, reference)),
            ("complete_task", "Mark a task completed.", IdArgs, lambda id: task_svc.complete_task(d, u, id)),
            ("delete_task", "Delete a task (destructive).", IdArgs, lambda id: task_svc.delete_task(d, u, id)),
            ("update_task", "Change a task's title, due date or priority.", UpdateTaskArgs,
             lambda id, **a: task_svc.update_task(d, u, id, **a)),
            ("save_website", "Save a website to memory.", SaveWebsiteArgs,
             lambda url, title="", notes=None: web_svc.save_website(d, u, url, title, notes)),
            ("search_websites", "Search saved websites.", SearchArgs,
             lambda query="", start=None, end=None, limit=20: web_svc.search_websites(d, u, query, start, end, limit)),
            ("create_file_record", "Store file metadata (and request a download).", FileIn,
             lambda **a: file_svc.create_file(d, u, FileIn(**a))),
            ("search_files", "Search saved file metadata.", SearchArgs,
             lambda query="", start=None, end=None, limit=20: file_svc.search_files(d, u, query, start, end, limit)),
        ]
        self.tools: dict[str, StructuredTool] = {
            name: StructuredTool.from_function(func=fn, name=name, description=desc, args_schema=schema)
            for name, desc, schema, fn in specs
        }

    def call(self, name: str, **kwargs) -> Any:
        tool = self.tools[name]
        args = {k: v for k, v in kwargs.items() if v is not None}
        for attempt in range(2):
            started = time.perf_counter()
            try:
                out = tool.invoke(args)
                self.deps.trace.append({"node": f"tool:{name}", "detail": f"{(time.perf_counter() - started) * 1000:.0f} ms", "ok": True})
                return out
            except OperationalError as exc:  # transient DB failure → rollback + one retry
                self.deps.db.rollback()
                log.warning("tool %s DB error (attempt %d): %s", name, attempt + 1, exc)
                if attempt == 1:
                    self.deps.trace.append({"node": f"tool:{name}", "detail": "database error", "ok": False})
                    raise
            except Exception as exc:
                self.deps.db.rollback()
                self.deps.trace.append({"node": f"tool:{name}", "detail": type(exc).__name__, "ok": False})
                raise
        raise RuntimeError("unreachable")

    def describe(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "args": t.args} for t in self.tools.values()]
