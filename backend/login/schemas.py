"""Pydantic schemas for login session endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class LoginSessionCreate(BaseModel):
    app_name: str
    login_url: str
    notes: str = ""


class LoginSessionUpdate(BaseModel):
    app_name: str | None = None
    login_url: str | None = None
    notes: str | None = None
    status: str | None = None


class LoginSessionOut(BaseModel):
    id: str
    app_name: str
    login_url: str
    status: str
    notes: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}
