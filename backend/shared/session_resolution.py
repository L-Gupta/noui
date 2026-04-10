"""Resolve domain session (login/workflow) from a capture_session_id.

The Chrome extension always sends a ``capture_session_id`` when posting
click events, URL events, and HAR files.  The login and workflow analysis
endpoints query by the *domain* session ID (``LoginSession.id`` or
``WorkflowSession.id``) with a matching ``session_type``.

This module bridges the gap: given a ``capture_session_id`` it walks
``CaptureSession → process_id → LoginSession / WorkflowSession`` and
returns the correct ``(session_id, session_type)`` pair so that data is
stored in a way the analysis endpoints can find it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def resolve_domain_session(
    capture_session_id: str, db: AsyncSession
) -> tuple[str, str] | None:
    """Return ``(domain_session_id, session_type)`` for a capture session.

    Returns ``None`` when the capture session has no linked domain session.
    """
    from backend.elicitation.models import CaptureSession
    from backend.login.models import LoginSession
    from backend.workflow.models import WorkflowSession

    r = await db.execute(
        select(CaptureSession).where(CaptureSession.id == capture_session_id)
    )
    cs = r.scalar_one_or_none()
    if not cs or not cs.process_id:
        return None

    # Check login first — login sessions are more specific
    lr = await db.execute(
        select(LoginSession).where(LoginSession.process_id == cs.process_id)
    )
    login = lr.scalar_one_or_none()
    if login:
        return login.id, "login"

    wr = await db.execute(
        select(WorkflowSession).where(WorkflowSession.process_id == cs.process_id)
    )
    workflow = wr.scalar_one_or_none()
    if workflow:
        return workflow.id, "workflow"

    return None
