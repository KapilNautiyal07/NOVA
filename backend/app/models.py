"""ORM models. All timestamps are stored in UTC (see UTCDateTime)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base, UTCDateTime, utcnow

JSONType = JSON().with_variant(JSONB(), "postgresql")

REMINDER_STATUSES = ("pending", "snoozed", "fired", "completed", "cancelled")
REMINDER_KINDS = ("reminder", "alarm", "meeting")
RECURRENCES = ("daily", "weekdays", "weekly", "monthly")
TASK_STATUSES = ("pending", "in_progress", "completed", "cancelled")
PRIORITIES = ("low", "medium", "high", "urgent")
FILE_STATUSES = ("saved", "requested", "in_progress", "complete", "failed", "interrupted")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    preferences: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    tokens: Mapped[list[ApiToken]] = relationship(back_populates="user", cascade="all, delete-orphan")


class ApiToken(Base):
    """Per-device bearer tokens. Only a SHA-256 hash is stored."""

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80), default="device")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates="tokens")


class SavedWebsite(Base):
    __tablename__ = "saved_websites"
    __table_args__ = (
        UniqueConstraint("user_id", "url", name="uq_website_user_url"),
        Index("ix_website_user_created", "user_id", "created_at"),
        Index("ix_website_user_visited", "user_id", "last_visited_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(500), default="")
    domain: Mapped[str] = mapped_column(String(255), index=True, default="")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    visit_count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_visited_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(_in("status", TASK_STATUSES), name="ck_task_status"),
        CheckConstraint(_in("priority", PRIORITIES), name="ck_task_priority"),
        Index("ix_task_user_status_due", "user_id", "status", "due_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    due_has_time: Mapped[bool] = mapped_column(default=False)
    priority: Mapped[str] = mapped_column(String(10), default="medium")
    status: Mapped[str] = mapped_column(String(12), default="pending")
    recurrence: Mapped[str | None] = mapped_column(String(10), nullable=True)
    related_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    reminders: Mapped[list[Reminder]] = relationship(back_populates="task")


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        CheckConstraint(_in("status", REMINDER_STATUSES), name="ck_reminder_status"),
        CheckConstraint(_in("kind", REMINDER_KINDS), name="ck_reminder_kind"),
        Index("ix_reminder_user_status_time", "user_id", "status", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(10), default="reminder")
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    recurrence: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Minutes *before* scheduled_at at which to notify, e.g. [15, 0] for a meeting.
    notify_offsets: Mapped[list] = mapped_column(JSONType, default=lambda: [0])
    status: Mapped[str] = mapped_column(String(10), default="pending")
    snoozed_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    related_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    website_id: Mapped[int | None] = mapped_column(
        ForeignKey("saved_websites.id", ondelete="SET NULL"), nullable=True
    )
    last_fired_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    task: Mapped[Task | None] = relationship(back_populates="reminders")

    @property
    def next_fire_at(self) -> datetime:
        return self.snoozed_until if self.status == "snoozed" and self.snoozed_until else self.scheduled_at


class SavedFile(Base):
    __tablename__ = "saved_files"
    __table_args__ = (
        CheckConstraint(_in("status", FILE_STATUSES), name="ck_file_status"),
        Index("ix_file_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    file_name: Mapped[str] = mapped_column(String(255))
    file_url: Mapped[str] = mapped_column(Text)
    file_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    folder: Mapped[str | None] = mapped_column(String(120), nullable=True)
    local_download_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="saved")
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_page_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_page_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("user_id", "session_id", name="uq_conversation_session"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(String(64))
    # Pending clarification / confirmation the agent is waiting on.
    state: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.id"
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="ck_message_role"),
        Index("ix_message_conversation", "conversation_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column("metadata", JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Integration(Base):
    """A third-party account the user explicitly connected (e.g. Google Calendar).

    OAuth tokens are stored encrypted (Fernet) and never returned by the API.
    """

    __tablename__ = "integrations"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_integration_user_provider"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40))  # "google_calendar"
    account_label: Mapped[str | None] = mapped_column(String(254), nullable=True)
    scopes: Mapped[str] = mapped_column(Text, default="")
    access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class OAuthState(Base):
    """One in-flight OAuth authorization (CSRF state + PKCE verifier), single use, short-lived."""

    __tablename__ = "oauth_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    code_verifier_enc: Mapped[str] = mapped_column(Text)
    # pending → connected | denied | error   (the extension polls this to finish the flow)
    outcome: Mapped[str] = mapped_column(String(20), default="pending")
    detail: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
