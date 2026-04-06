"""Shared SQLAlchemy ORM models used by both login and workflow recording paths."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid.uuid4())


class ClickEvent(Base):
    __tablename__ = "click_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    # Session association (replaces abcd's project_id / process_id FKs)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    session_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # login | workflow

    event_type: Mapped[str] = mapped_column(String(20), default="click")  # click | input | change | submit
    url: Mapped[str] = mapped_column(String(2000), default="")
    tag_name: Mapped[str] = mapped_column(String(50))
    element_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    class_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    text_content: Mapped[str | None] = mapped_column(String(100), nullable=True)
    href: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    selector: Mapped[str] = mapped_column(String(500), default="")
    x: Mapped[int] = mapped_column(Integer, default=0)
    y: Mapped[int] = mapped_column(Integer, default=0)
    input_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    field_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    field_role: Mapped[str | None] = mapped_column(String(30), nullable=True)  # username | password | otp | unknown_sensitive
    is_redacted: Mapped[bool] = mapped_column(Boolean, default=False)
    autocomplete: Mapped[str | None] = mapped_column(String(100), nullable=True)
    placeholder: Mapped[str | None] = mapped_column(String(255), nullable=True)
    aria_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role_attr: Mapped[str | None] = mapped_column(String(50), nullable=True)
    data_attrs_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: {"data-testid": "..."}
    timestamp: Mapped[datetime] = mapped_column(default=_utcnow)


class UrlEvent(Base):
    """Records URL navigation events for both login and workflow sessions."""

    __tablename__ = "url_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    session_type: Mapped[str] = mapped_column(String(20), nullable=False)  # login | workflow
    from_url: Mapped[str] = mapped_column(String(2000), default="")
    to_url: Mapped[str] = mapped_column(String(2000), default="")
    timestamp: Mapped[datetime] = mapped_column(default=_utcnow)


class HarFile(Base):
    """Records the path to an uploaded HAR file for a session."""

    __tablename__ = "har_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    session_type: Mapped[str] = mapped_column(String(20), nullable=False)  # login | workflow
    file_path: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
