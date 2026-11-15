from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import and_, case, or_, select
from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import Task, User
from ..schemas import TaskIn
from ..timeparse import get_tz, next_occurrence
from .common import NotFound, search_tokens

OPEN = ("pending", "in_progress")
_PRIORITY_ORDER = case(
    {"urgent": 0, "high": 1, "medium": 2, "low": 3}, value=Task.priority, else_=4
)


def create_task(db: Session, user: User, data: TaskIn) -> Task:
    task = Task(
        user_id=user.id,
        title=data.title.strip(),
        description=data.description,
        due_at=data.due_at,
        due_has_time=data.due_has_time,
        priority=data.priority,
        recurrence=data.recurrence,
        related_url=data.related_url,
        related_title=data.related_title,
        status="pending",
    )
    db.add(task)
    db.commit()
    return task


def get_task(db: Session, user: User, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if not task or task.user_id != user.id:
        raise NotFound("task", task_id)
    return task


def is_overdue(task: Task, now: datetime) -> bool:
    """`now` must be aware and in the user's timezone. Date-only tasks are overdue the day after."""
    if task.due_at is None or task.status not in OPEN:
        return False
    if task.due_has_time:
        return task.due_at < now
    return task.due_at.astimezone(now.tzinfo).date() < now.date()


def day_bounds(user: User, day_offset: int = 0) -> tuple[datetime, datetime]:
    tz = get_tz(user.timezone)
    today = datetime.now(tz).date() + timedelta(days=day_offset)
    start = datetime.combine(today, time(0)).replace(tzinfo=tz)
    return start, start + timedelta(days=1)


def list_tasks(
    db: Session,
    user: User,
    statuses: list[str] | None = None,
    priority: str | None = None,
    due: str | None = None,  # today | tomorrow | upcoming | overdue | none
    query: str | None = None,
    limit: int = 100,
) -> list[Task]:
    stmt = select(Task).where(Task.user_id == user.id)
    if statuses:
        stmt = stmt.where(Task.status.in_(statuses))
    if priority:
        stmt = stmt.where(Task.priority == priority)
    now = utcnow()
    if due == "today":
        start, end = day_bounds(user)
        stmt = stmt.where(Task.due_at < end)  # due today, or overdue and still listed
        if not statuses:
            stmt = stmt.where(Task.status.in_(OPEN))
    elif due == "tomorrow":
        start, end = day_bounds(user, 1)
        stmt = stmt.where(Task.due_at >= start, Task.due_at < end)
    elif due == "upcoming":
        stmt = stmt.where(Task.due_at >= now)
    elif due == "overdue":
        # A date-only task ("due Friday") is only overdue once that whole day has passed.
        start_today, _ = day_bounds(user)
        stmt = stmt.where(
            or_(and_(Task.due_has_time.is_(True), Task.due_at < now),
                and_(Task.due_has_time.is_(False), Task.due_at < start_today)),
            Task.status.in_(OPEN),
        )
    elif due == "none":
        stmt = stmt.where(Task.due_at.is_(None))
    stmt = stmt.order_by(Task.due_at.is_(None), Task.due_at.asc(), _PRIORITY_ORDER, Task.created_at.desc())
    tasks = list(db.scalars(stmt.limit(500)))
    if query:
        tokens = search_tokens(query)
        if tokens:
            tasks = [t for t in tasks if any(tok in f"{t.title} {t.description or ''}".lower() for tok in tokens)]
    return tasks[:limit]


def update_task(db: Session, user: User, task_id: int, **fields) -> Task:
    task = get_task(db, user, task_id)
    old_due, old_title = task.due_at, task.title
    for k, v in fields.items():
        if v is not None:
            setattr(task, k, v)
    if fields.get("status") == "completed" and not task.completed_at:
        task.completed_at = utcnow()
    elif fields.get("status") in OPEN:
        task.completed_at = None
    _sync_linked_reminders(task, user, old_due, old_title)
    db.commit()
    return task


def _sync_linked_reminders(task: Task, user: User, old_due: datetime | None, old_title: str) -> None:
    """Keep a task's reminders consistent with it: rescheduling the task moves them,
    renaming it renames them. Otherwise the notification would fire at the old time."""
    if task.due_at is not None and task.due_at != old_due:
        now = utcnow()
        for rem in task.reminders:
            if rem.status not in ("pending", "snoozed", "fired"):
                continue
            if task.due_has_time:
                new_at = task.due_at
            else:  # date-only due: keep the reminder's local time of day on the new date
                rem_tz = get_tz(rem.timezone)
                new_day = task.due_at.astimezone(get_tz(user.timezone)).date()
                new_at = datetime.combine(new_day, rem.scheduled_at.astimezone(rem_tz).time()).replace(tzinfo=rem_tz)
            if new_at > now:
                rem.scheduled_at = new_at
                rem.status, rem.snoozed_until = "pending", None
    if task.title != old_title:
        for rem in task.reminders:
            if rem.title == old_title:
                rem.title = task.title


def complete_task(db: Session, user: User, task_id: int) -> tuple[Task, Task | None]:
    """Completes a task. For recurring tasks, also creates the next occurrence."""
    task = get_task(db, user, task_id)
    task.status = "completed"
    task.completed_at = utcnow()
    nxt = None
    if task.recurrence and task.due_at:
        nxt = Task(
            user_id=user.id,
            title=task.title,
            description=task.description,
            due_at=next_occurrence(task.due_at, task.recurrence, user.timezone, after=utcnow()),
            due_has_time=task.due_has_time,
            priority=task.priority,
            recurrence=task.recurrence,
            related_url=task.related_url,
            related_title=task.related_title,
        )
        db.add(nxt)
    # Completing a task also completes its pending reminders.
    for rem in task.reminders:
        if rem.status in ("pending", "snoozed", "fired") and not rem.recurrence:
            rem.status = "completed"
            rem.completed_at = utcnow()
    db.commit()
    return task, nxt


def delete_task(db: Session, user: User, task_id: int) -> None:
    task = get_task(db, user, task_id)
    for rem in task.reminders:
        if rem.status in ("pending", "snoozed"):
            rem.status = "cancelled"
    db.delete(task)
    db.commit()


def find_tasks(db: Session, user: User, reference: str, include_done: bool = False) -> list[Task]:
    statuses = None if include_done else list(OPEN)
    tasks = list_tasks(db, user, statuses=statuses, limit=500)
    tokens = search_tokens(reference)
    if not tokens:
        return []
    scored = []
    for t in tasks:
        hay = f"{t.title} {t.description or ''}".lower()
        s = sum(tok in hay for tok in tokens)
        if s:
            scored.append((s / len(tokens), t))
    if not scored:
        return []
    best = max(s for s, _ in scored)
    return [t for s, t in scored if s == best]
