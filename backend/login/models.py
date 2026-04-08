"""ORM models specific to the login-recording path."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_uuid() -> str:
    return str(uuid.uuid4())


class LoginSession(Base):
    """A login-recording session initiated by the Chrome extension."""

    __tablename__ = "login_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    app_name: Mapped[str] = mapped_column(String(255))
    login_url: Mapped[str] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(
        String(20), default="idle"
    )  # idle | recording | completed | failed
    notes: Mapped[str] = mapped_column(Text, default="")
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    process_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)


class LoginDraft(Base):
    """Generated Tabby bundle for a login session."""

    __tablename__ = "login_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    login_session_id: Mapped[str] = mapped_column(String(36))  # references login_sessions.id
    bundle_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
