"""Pydantic schemas for workflow session endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class WorkflowSessionCreate(BaseModel):
    name: str
    start_url: str = ""
    description: str = ""


class WorkflowSessionUpdate(BaseModel):
    name: str | None = None
    start_url: str | None = None
    description: str | None = None
    status: str | None = None


class WorkflowSessionOut(BaseModel):
    id: str
    name: str
    start_url: str
    description: str
    status: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}
