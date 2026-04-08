"""Pydantic schemas for autopilot recording endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class AutopilotRunCreate(BaseModel):
    """Create a new autopilot run record.

    Credentials are never sent through the API — they go through the
    Claude Code skill prompt and are typed directly into the browser.
    """

    website_url: str
    task_description: str
    login_url: str = ""
    tabby_profile_id: str = ""
    success_condition: str = ""
    stop_condition: str = ""
    allowed_side_effects: list[str] = []
    forbidden_side_effects: list[str] = []
    test_data: dict = {}
    mfa_policy: str = "pause_for_user"
    max_browser_steps: int = 80
    max_duration_seconds: int = 600


class AutopilotRunOut(BaseModel):
    id: str
    status: str
    website_url: str
    login_url: str
    task_description: str
    success_condition: str
    stop_condition: str
    tabby_profile_id: str
    login_session_id: str | None
    workflow_session_id: str | None
    capture_session_id: str | None
    server_id: str
    mcp_output_path: str
    agent_trace_path: str
    tools_count: int
    failure_reason: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}
