"""ORM model for autopilot recording runs."""

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


class AutopilotRecordingRun(Base):
    """Tracks an end-to-end autopilot recording run.

    Credentials are never stored here -- only secret refs or nothing.
    """

    __tablename__ = "autopilot_recording_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)

    # Status lifecycle:
    #   queued -> preparing_auth -> logging_in -> recording_workflow
    #   -> validating_capture -> exporting_mcp -> completed
    #   Any state can transition to: needs_user | failed
    status: Mapped[str] = mapped_column(String(30), default="queued")

    # Request fields
    website_url: Mapped[str] = mapped_column(String(2000))
    login_url: Mapped[str] = mapped_column(String(2000), default="")
    task_description: Mapped[str] = mapped_column(Text)
    success_condition: Mapped[str] = mapped_column(Text, default="")
    stop_condition: Mapped[str] = mapped_column(Text, default="")
    allowed_side_effects_json: Mapped[str] = mapped_column(Text, default="[]")
    forbidden_side_effects_json: Mapped[str] = mapped_column(Text, default="[]")
    test_data_json: Mapped[str] = mapped_column(Text, default="{}")
    mfa_policy: Mapped[str] = mapped_column(String(30), default="pause_for_user")
    max_browser_steps: Mapped[int] = mapped_column(default=80)
    max_duration_seconds: Mapped[int] = mapped_column(default=600)

    # Linked session IDs
    tabby_profile_id: Mapped[str] = mapped_column(String(255), default="")
    login_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    workflow_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    capture_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Output
    server_id: Mapped[str] = mapped_column(String(255), default="")
    mcp_output_path: Mapped[str] = mapped_column(String(2000), default="")
    agent_trace_path: Mapped[str] = mapped_column(String(2000), default="")
    tools_count: Mapped[int] = mapped_column(default=0)

    # Failure
    failure_reason: Mapped[str] = mapped_column(Text, default="")

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
