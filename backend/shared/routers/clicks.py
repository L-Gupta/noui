"""Shared router for click events captured by the Chrome extension content script."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.shared.models import ClickEvent
from backend.shared.schemas import ClickEventCreate, ClickEventOut

router = APIRouter(prefix="/clicks", tags=["clicks"])


@router.post("", response_model=ClickEventOut, status_code=201)
async def create_click(data: ClickEventCreate, db: AsyncSession = Depends(get_db)) -> ClickEvent:
    """Store a single click/input/change/submit event."""
    click = ClickEvent(
        session_id=data.session_id,
        session_type=data.session_type,
        event_type=data.event_type,
        url=data.url,
        tag_name=data.tag_name,
        element_id=data.element_id,
        class_name=data.class_name,
        text_content=data.text_content,
        href=data.href,
        selector=data.selector,
        x=data.x,
        y=data.y,
        input_type=data.input_type,
        value=data.value,
        field_name=data.field_name,
        field_role=data.field_role,
        is_redacted=data.is_redacted,
        autocomplete=data.autocomplete,
        placeholder=data.placeholder,
        aria_label=data.aria_label,
        role_attr=data.role_attr,
        data_attrs_json=data.data_attrs_json,
    )
    if data.timestamp:
        click.timestamp = data.timestamp
    db.add(click)
    await db.commit()
    await db.refresh(click)
    return click


@router.get("/sessions/{session_id}/clicks", response_model=list[ClickEventOut])
async def list_session_clicks(
    session_id: str,
    session_type: str = Query(..., description="login or workflow"),
    limit: int = Query(500, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
) -> list[ClickEvent]:
    """List all click events for a given session."""
    stmt = (
        select(ClickEvent)
        .where(
            ClickEvent.session_id == session_id,
            ClickEvent.session_type == session_type,
        )
        .order_by(ClickEvent.timestamp.asc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/{click_id}", response_model=ClickEventOut)
async def get_click(click_id: str, db: AsyncSession = Depends(get_db)) -> ClickEvent:
    """Retrieve a single click event by ID."""
    result = await db.execute(select(ClickEvent).where(ClickEvent.id == click_id))
    click = result.scalar_one_or_none()
    if not click:
        raise HTTPException(status_code=404, detail="Click event not found")
    return click
