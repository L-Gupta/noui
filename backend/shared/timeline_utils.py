"""Helpers for emitting URL events and other session timeline entries."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from backend.shared.models import UrlEvent

logger = logging.getLogger(__name__)


async def emit_url_event(
    db: AsyncSession,
    session_id: str,
    session_type: str,
    from_url: str,
    to_url: str,
    timestamp: datetime | None = None,
) -> UrlEvent:
    """Create a URL navigation event in the database.

    Args:
        db: Async SQLAlchemy session (caller is responsible for commit).
        session_id: ID of the login or workflow session.
        session_type: "login" or "workflow".
        from_url: The URL navigated away from (may be empty string).
        to_url: The URL navigated to.
        timestamp: Optional explicit timestamp; defaults to utcnow.

    Returns:
        The persisted UrlEvent instance (flushed but not committed).
    """
    event = UrlEvent(
        session_id=session_id,
        session_type=session_type,
        from_url=from_url,
        to_url=to_url,
    )
    if timestamp:
        event.timestamp = timestamp
    db.add(event)
    await db.flush()  # Use flush so caller can batch with other writes
    return event
