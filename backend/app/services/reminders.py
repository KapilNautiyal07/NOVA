from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import Reminder, User
from ..schemas import ReminderIn
from ..timeparse import get_tz, next_occurrence, parse_when
from .common import NotFound, search_tokens

ACTIVE = ("pending", "snoozed")


def create_reminder(db: Session, user: User, data: ReminderIn, website_id: int | None = None) -> Reminder:
    rem = Reminder(
        user_id=user.id,
        title=data.title.strip(),
        description=data.description,
        kind=data.kind,
        scheduled_at=data.scheduled_at,
        timezone=data.timezone or user.timezone,
        recurrence=data.recurrence,
        notify_offsets=data.notify_offsets,
        related_url=data.related_url,
        related_title=data.related_title,
        task_id=data.task_id,
        website_id=website_id,
        status="pending",
    )
    db.add(rem)
    db.commit()
    return rem


def get_reminder(db: Session, user: User, reminder_id: int) -> Reminder:
    rem = db.get(Reminder, reminder_id)
    if not rem or rem.user_id != user.id:
        raise NotFound("reminder", reminder_id)
    return rem


def list_reminders(
    db: Session,
    user: User,
    statuses: list[str] | None = None,
    kind: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 100,
) -> list[Reminder]:
    stmt = select(Reminder).where(Reminder.user_id == user.id)
    if statuses:
        stmt = stmt.where(Reminder.status.in_(statuses))
    if kind:
        stmt = stmt.where(Reminder.kind == kind)
    if start:
        stmt = stmt.where(Reminder.scheduled_at >= start)
    if end:
        stmt = stmt.where(Reminder.scheduled_at < end)
    order = Reminder.scheduled_at.asc() if not statuses or set(statuses) <= set(ACTIVE) else Reminder.scheduled_at.desc()
    return list(db.scalars(stmt.order_by(order).limit(limit)))


def update_reminder(db: Session, user: User, reminder_id: int, **fields) -> Reminder:
    rem = get_reminder(db, user, reminder_id)
    for k, v in fields.items():
        if v is not None:
            setattr(rem, k, v)
    if fields.get("scheduled_at") is not None and rem.status in ("snoozed", "fired"):
        rem.status, rem.snoozed_until = "pending", None
    if fields.get("status") == "completed":
        rem.completed_at = utcnow()
    db.commit()
    return rem


def snooze_reminder(db: Session, user: User, reminder_id: int, minutes: int) -> Reminder:
    rem = get_reminder(db, user, reminder_id)
    if rem.status in ("completed", "cancelled"):
        raise ValueError(f"cannot snooze a {rem.status} reminder")
    rem.status = "snoozed"
    rem.snoozed_until = utcnow() + timedelta(minutes=minutes)
    db.commit()
    return rem


def complete_reminder(db: Session, user: User, reminder_id: int) -> Reminder:
    rem = get_reminder(db, user, reminder_id)
    if rem.recurrence:
        # Completing one occurrence of a recurring reminder advances it.
        _advance(rem)
    else:
        rem.status = "completed"
        rem.completed_at = utcnow()
        rem.snoozed_until = None
    db.commit()
    return rem


def cancel_reminder(db: Session, user: User, reminder_id: int) -> Reminder:
    rem = get_reminder(db, user, reminder_id)
    rem.status = "cancelled"
    rem.snoozed_until = None
    db.commit()
    return rem


def delete_reminder(db: Session, user: User, reminder_id: int) -> None:
    db.delete(get_reminder(db, user, reminder_id))
    db.commit()


def _advance(rem: Reminder) -> None:
    rem.scheduled_at = next_occurrence(rem.scheduled_at, rem.recurrence, rem.timezone, after=utcnow())
    rem.status = "pending"
    rem.snoozed_until = None


def mark_fired(db: Session, user: User, reminder_id: int, fired_at: datetime | None = None) -> Reminder:
    """Called by the extension when the notification for the final offset was shown."""
    rem = get_reminder(db, user, reminder_id)
    rem.last_fired_at = fired_at or utcnow()
    if rem.status in ("completed", "cancelled"):
        db.commit()
        return rem
    if rem.recurrence:
        _advance(rem)
    else:
        rem.status = "fired"
        rem.snoozed_until = None
    db.commit()
    return rem


def find_reminders(db: Session, user: User, reference: str, now_local: datetime) -> list[Reminder]:
    """Resolve "my 5 PM reminder" / "the dentist reminder" to active reminders."""
    active = list_reminders(db, user, statuses=list(ACTIVE), limit=500)
    if not active:
        return []
    tz = get_tz(user.timezone)
    parsed = parse_when(reference, now_local) if reference else None
    if parsed and parsed.has_time:
        target = parsed.dt
        hits = [
            r
            for r in active
            if (lt := r.next_fire_at.astimezone(tz)).hour == target.hour and lt.minute == target.minute
            and (not parsed.has_date or lt.date() == target.date())
        ]
        if hits:
            return sorted(hits, key=lambda r: r.next_fire_at)
    tokens = search_tokens(reference)
    if tokens:
        scored = []
        for r in active:
            hay = f"{r.title} {r.description or ''} {r.related_title or ''}".lower()
            s = sum(tok in hay for tok in tokens)
            if s:
                scored.append((s, r))
        if scored:
            best = max(s for s, _ in scored)
            return [r for s, r in sorted(scored, key=lambda x: x[1].next_fire_at) if s == best]
    return []


def count_active(db: Session, user: User) -> int:
    return db.scalar(
        select(func.count()).select_from(Reminder).where(Reminder.user_id == user.id, Reminder.status.in_(ACTIVE))
    ) or 0
