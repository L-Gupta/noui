"""Pydantic request/response schemas for shared models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, model_validator

# ── ClickEvent ───────────────────────────────────────────────────────────────


class ClickEventCreate(BaseModel):
    # Accept both the NoUI field name (session_id) and the abcd extension alias
    session_id: str | None = None
    capture_session_id: str | None = None  # extension compat alias for session_id
    session_type: str | None = None  # login | workflow

    @model_validator(mode="before")
    @classmethod
    def _normalize_session_id(cls, data: dict) -> dict:
        if isinstance(data, dict):
            if not data.get("session_id") and data.get("capture_session_id"):
                data["session_id"] = data["capture_session_id"]
        return data

    event_type: str = "click"  # click | input | change | submit
    url: str = ""
    tag_name: str
    element_id: str | None = None
    class_name: str | None = None
    text_content: str | None = None
    href: str | None = None
    selector: str = ""
    x: int = 0
    y: int = 0
    input_type: str | None = None
    value: str | None = None
    field_name: str | None = None
    field_role: str | None = None
    is_redacted: bool = False
    autocomplete: str | None = None
    placeholder: str | None = None
    aria_label: str | None = None
    role_attr: str | None = None
    data_attrs_json: str | None = None
    timestamp: datetime | None = None


class ClickEventOut(BaseModel):
    id: str
    session_id: str | None
    session_type: str | None
    event_type: str
    url: str
    tag_name: str
    element_id: str | None
    class_name: str | None
    text_content: str | None
    href: str | None
    selector: str
    x: int
    y: int
    input_type: str | None
    value: str | None
    field_name: str | None
    field_role: str | None
    is_redacted: bool
    autocomplete: str | None
    placeholder: str | None
    aria_label: str | None
    role_attr: str | None
    data_attrs_json: str | None
    timestamp: datetime

    model_config = {"from_attributes": True}


# ── UrlEvent ─────────────────────────────────────────────────────────────────


class UrlEventCreate(BaseModel):
    session_id: str | None = None
    capture_session_id: str | None = None  # extension compat alias for session_id
    session_type: str | None = None  # login | workflow (resolved server-side when absent)
    from_url: str = ""
    to_url: str

    @model_validator(mode="before")
    @classmethod
    def _normalize_session_id(cls, data: dict) -> dict:
        if isinstance(data, dict):
            if not data.get("session_id") and data.get("capture_session_id"):
                data["session_id"] = data["capture_session_id"]
        return data


class UrlEventOut(BaseModel):
    id: str
    session_id: str
    session_type: str
    from_url: str
    to_url: str
    timestamp: datetime

    model_config = {"from_attributes": True}


# ── HarFile ──────────────────────────────────────────────────────────────────


class HarFileOut(BaseModel):
    id: str
    session_id: str
    session_type: str
    file_path: str
    created_at: datetime

    model_config = {"from_attributes": True}
