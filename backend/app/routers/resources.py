"""REST endpoints for reminders, tasks, websites and files (used by the dashboard)."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..schemas import (
    FileIn, FileOut, FileStatusUpdate, FileUpdate, ReminderIn, ReminderOut, ReminderUpdate, SnoozeIn,
    TaskIn, TaskOut, TaskUpdate, VisitIn, WebsiteIn, WebsiteOut, WebsiteUpdate,
)
from ..security import current_user
from ..services import files as file_svc
from ..services import reminders as rem_svc
from ..services import tasks as task_svc
from ..services import websites as web_svc
from ..timeparse import get_tz, parse_period

router = APIRouter(prefix="/api", tags=["resources"])


# ------------------------------------------------------------------ reminders
@router.get("/reminders", response_model=list[ReminderOut])
def list_reminders(
    status: list[str] | None = Query(default=None),
    kind: str | None = None,
    limit: int = Query(default=100, le=500),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return rem_svc.list_reminders(db, user, statuses=status, kind=kind, limit=limit)


@router.post("/reminders", response_model=ReminderOut, status_code=201)
def create_reminder(body: ReminderIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return rem_svc.create_reminder(db, user, body)


@router.get("/reminders/{reminder_id}", response_model=ReminderOut)
def get_reminder(reminder_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return rem_svc.get_reminder(db, user, reminder_id)


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return task_svc.get_task(db, user, task_id)


@router.get("/websites/{website_id}", response_model=WebsiteOut)
def get_website(website_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return web_svc.get_website(db, user, website_id)


@router.get("/files/{file_id}", response_model=FileOut)
def get_file(file_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return file_svc.get_file(db, user, file_id)


@router.patch("/reminders/{reminder_id}", response_model=ReminderOut)
def update_reminder(reminder_id: int, body: ReminderUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return rem_svc.update_reminder(db, user, reminder_id, **body.model_dump(exclude_unset=True))


@router.post("/reminders/{reminder_id}/snooze", response_model=ReminderOut)
def snooze(reminder_id: int, body: SnoozeIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        return rem_svc.snooze_reminder(db, user, reminder_id, body.minutes)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/reminders/{reminder_id}/complete", response_model=ReminderOut)
def complete_reminder(reminder_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return rem_svc.complete_reminder(db, user, reminder_id)


@router.post("/reminders/{reminder_id}/fired", response_model=ReminderOut)
def fired(reminder_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return rem_svc.mark_fired(db, user, reminder_id)


@router.delete("/reminders/{reminder_id}", status_code=204)
def delete_reminder(reminder_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rem_svc.delete_reminder(db, user, reminder_id)
    return Response(status_code=204)


# ------------------------------------------------------------------ tasks
@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(
    status: list[str] | None = Query(default=None),
    priority: str | None = None,
    due: str | None = Query(default=None, pattern="^(today|tomorrow|upcoming|overdue|none)$"),
    q: str | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return task_svc.list_tasks(db, user, statuses=status, priority=priority, due=due, query=q)


@router.post("/tasks", response_model=TaskOut, status_code=201)
def create_task(body: TaskIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return task_svc.create_task(db, user, body)


@router.patch("/tasks/{task_id}", response_model=TaskOut)
def update_task(task_id: int, body: TaskUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    fields = body.model_dump(exclude_unset=True)
    if fields.get("status") == "completed":
        task, _ = task_svc.complete_task(db, user, task_id)
        fields.pop("status")
        if not fields:
            return task
    return task_svc.update_task(db, user, task_id, **fields)


@router.post("/tasks/{task_id}/complete", response_model=TaskOut)
def complete_task(task_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    task, _ = task_svc.complete_task(db, user, task_id)
    return task


@router.delete("/tasks/{task_id}", status_code=204)
def delete_task(task_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    task_svc.delete_task(db, user, task_id)
    return Response(status_code=204)


# ------------------------------------------------------------------ websites
@router.get("/websites", response_model=list[WebsiteOut])
def list_websites(
    q: str | None = None,
    period: str | None = None,
    category: str | None = None,
    limit: int = Query(default=50, le=200),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if q or period:
        start = end = None
        if period:
            parsed = parse_period(period, datetime.now(get_tz(user.timezone)))
            if parsed:
                start, end, _ = parsed
        return web_svc.search_websites(db, user, q or "", start, end, limit)
    return web_svc.list_websites(db, user, category=category, limit=limit)


@router.post("/websites", response_model=WebsiteOut, status_code=201)
def save_website(body: WebsiteIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    site, _ = web_svc.save_website(db, user, body.url, body.title, body.notes, body.category)
    return site


@router.post("/websites/visit")
def record_visit(body: VisitIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    site = web_svc.record_visit(db, user, body.url, body.title)
    return {"tracked": site is not None, "id": site.id if site else None}


@router.patch("/websites/{website_id}", response_model=WebsiteOut)
def update_website(website_id: int, body: WebsiteUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return web_svc.update_website(db, user, website_id, **body.model_dump(exclude_unset=True))


@router.delete("/websites/{website_id}", status_code=204)
def delete_website(website_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    web_svc.delete_website(db, user, website_id)
    return Response(status_code=204)


# ------------------------------------------------------------------ files
@router.get("/files", response_model=list[FileOut])
def list_files(q: str | None = None, period: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    start = end = None
    if period:
        parsed = parse_period(period, datetime.now(get_tz(user.timezone)))
        if parsed:
            start, end, _ = parsed
    return file_svc.search_files(db, user, q or "", start, end)


@router.post("/files", response_model=FileOut, status_code=201)
def create_file(body: FileIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return file_svc.create_file(db, user, body)


@router.patch("/files/{file_id}", response_model=FileOut)
def update_file(file_id: int, body: FileUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return file_svc.update_file(db, user, file_id, **body.model_dump(exclude_unset=True))


@router.patch("/files/{file_id}/status", response_model=FileOut)
def update_file_status(file_id: int, body: FileStatusUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return file_svc.update_file(db, user, file_id, **body.model_dump(exclude_unset=True))


@router.delete("/files/{file_id}", status_code=204)
def delete_file(file_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    file_svc.delete_file(db, user, file_id)
    return Response(status_code=204)
