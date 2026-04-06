"""Shared router for URL navigation events."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.shared.models import UrlEvent
from backend.shared.schemas import UrlEventCreate, UrlEventOut
from backend.shared.timeline_utils import emit_url_event

router = APIRouter(prefix="/url-events", tags=["url-events"])


@router.post("", response_model=UrlEventOut, status_code=201)
async def create_url_event(data: UrlEventCreate, db: AsyncSession = Depends(get_db)) -> UrlEvent:
    """Store a URL navigation event.

    ``session_type`` is optional when the extension sends ``capture_session_id``
    — the server resolves it by looking up the ID in login/workflow tables.
    """
    session_id = data.session_id or ""
    session_type = data.session_type

    if session_id and not session_type:
        # Lazy import to avoid circular dependency at module level
        from backend.shared.routers.har import _resolve_session_type
        session_type = await _resolve_session_type(session_id, db) or "unknown"

    event = await emit_url_event(
        db,
        session_id=session_id,
        session_type=session_type or "unknown",
        from_url=data.from_url,
        to_url=data.to_url,
    )
    await db.commit()
    await db.refresh(event)
    return event


@router.get("/sessions/{session_id}/url-events", response_model=list[UrlEventOut])
async def list_session_url_events(
    session_id: str,
    session_type: str = Query(..., description="login or workflow"),
    limit: int = Query(500, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
) -> list[UrlEvent]:
    """List all URL navigation events for a given session."""
    stmt = (
        select(UrlEvent)
        .where(
            UrlEvent.session_id == session_id,
            UrlEvent.session_type == session_type,
        )
        .order_by(UrlEvent.timestamp.asc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())
