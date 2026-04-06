"""Routes for login-recording sessions."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.login.models import LoginDraft, LoginSession
from backend.login.schemas import LoginSessionCreate, LoginSessionOut, LoginSessionUpdate
from backend.shared.models import ClickEvent, HarFile, UrlEvent

logger = logging.getLogger(__name__)

router = APIRouter(tags=["login-sessions"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_session(session_id: str, db: AsyncSession) -> LoginSession:
    result = await db.execute(select(LoginSession).where(LoginSession.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Login session not found")
    return session


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.post("", response_model=LoginSessionOut, status_code=201)
async def create_login_session(
    data: LoginSessionCreate,
    db: AsyncSession = Depends(get_db),
) -> LoginSession:
    """Create a new login-recording session."""
    session = LoginSession(
        app_name=data.app_name,
        login_url=data.login_url,
        notes=data.notes,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    logger.info("Created login session %s for %s", session.id, session.app_name)
    return session


@router.get("", response_model=list[LoginSessionOut])
async def list_login_sessions(db: AsyncSession = Depends(get_db)) -> list[LoginSession]:
    """List all login sessions, newest first."""
    result = await db.execute(
        select(LoginSession).order_by(LoginSession.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/{session_id}", response_model=LoginSessionOut)
async def get_login_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> LoginSession:
    return await _get_session(session_id, db)


@router.patch("/{session_id}", response_model=LoginSessionOut)
async def update_login_session(
    session_id: str,
    data: LoginSessionUpdate,
    db: AsyncSession = Depends(get_db),
) -> LoginSession:
    """Partial-update a login session."""
    session = await _get_session(session_id, db)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(session, field, value)
    await db.commit()
    await db.refresh(session)
    return session


@router.post("/{session_id}/start", response_model=LoginSessionOut)
async def start_login_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> LoginSession:
    """Mark a login session as actively recording."""
    session = await _get_session(session_id, db)
    if session.status not in ("idle",):
        raise HTTPException(status_code=409, detail=f"Session is already in status '{session.status}'")
    session.status = "recording"
    session.started_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(session)
    return session


@router.post("/{session_id}/complete", response_model=LoginSessionOut)
async def complete_login_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> LoginSession:
    """Mark a login session as completed."""
    session = await _get_session(session_id, db)
    session.status = "completed"
    session.completed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(session)
    logger.info("Completed login session %s", session_id)
    return session


@router.delete("/{session_id}", status_code=204)
async def delete_login_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a login session and all its associated data."""
    session = await _get_session(session_id, db)
    await db.delete(session)
    await db.commit()


# ---------------------------------------------------------------------------
# Compiler endpoints
# ---------------------------------------------------------------------------

@router.post("/{session_id}/analyze")
async def analyze_login_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Run the login profile generator on this session's recorded events.

    - Loads ClickEvent records (session_type='login', chronological)
    - Loads UrlEvent records (session_type='login', chronological)
    - Loads HAR JSON from HarFile.file_path if a record exists
    - Runs compiler.login.tabby_draft_generator.generate()
    - Upserts LoginDraft with the resulting bundle
    - Returns the bundle dict
    """
    # Lazy import keeps the compiler package decoupled from the FastAPI startup path
    from compiler.login import tabby_draft_generator  # noqa: PLC0415

    login_session = await _get_session(session_id, db)

    # Load click events in chronological order
    click_result = await db.execute(
        select(ClickEvent)
        .where(ClickEvent.session_id == session_id, ClickEvent.session_type == "login")
        .order_by(ClickEvent.timestamp.asc())
    )
    click_events = [
        {c.key: getattr(row, c.key) for c in row.__table__.columns}
        for row in click_result.scalars().all()
    ]

    # Load URL events in chronological order
    url_result = await db.execute(
        select(UrlEvent)
        .where(UrlEvent.session_id == session_id, UrlEvent.session_type == "login")
        .order_by(UrlEvent.timestamp.asc())
    )
    url_events = [
        {c.key: getattr(row, c.key) for c in row.__table__.columns}
        for row in url_result.scalars().all()
    ]

    # Load most-recent HAR file if present
    har: dict | None = None
    har_result = await db.execute(
        select(HarFile)
        .where(HarFile.session_id == session_id, HarFile.session_type == "login")
        .order_by(HarFile.created_at.desc())
        .limit(1)
    )
    har_file = har_result.scalar_one_or_none()
    if har_file:
        try:
            with open(har_file.file_path, "r", encoding="utf-8") as fh:
                har = json.load(fh)
        except Exception as exc:
            logger.warning("Could not load HAR file %s: %s", har_file.file_path, exc)

    # Build a plain dict from ORM fields for the generator
    session_dict = {
        "id": login_session.id,
        "app_name": login_session.app_name,
        "login_url": login_session.login_url,
        "status": login_session.status,
        "started_at": login_session.started_at.isoformat() if login_session.started_at else None,
        "completed_at": login_session.completed_at.isoformat() if login_session.completed_at else None,
    }

    # Run the generator
    bundle = tabby_draft_generator.generate(
        session=session_dict,
        click_events=click_events,
        url_events=url_events,
        har=har,
    )

    bundle_json_str = json.dumps(bundle)

    # Upsert LoginDraft
    existing_result = await db.execute(
        select(LoginDraft).where(LoginDraft.login_session_id == session_id)
    )
    draft = existing_result.scalar_one_or_none()
    if draft is None:
        draft = LoginDraft(login_session_id=session_id, bundle_json=bundle_json_str)
        db.add(draft)
    else:
        draft.bundle_json = bundle_json_str

    await db.commit()
    await db.refresh(draft)

    logger.info("Analyzed login session %s — %d review items", session_id, len(bundle.get("review_items", [])))
    return bundle


@router.get("/{session_id}/bundle")
async def get_login_bundle(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return the previously generated bundle for this session."""
    # Verify the session exists so unknown IDs return 404, not just "no bundle"
    await _get_session(session_id, db)

    result = await db.execute(
        select(LoginDraft).where(LoginDraft.login_session_id == session_id)
    )
    draft = result.scalar_one_or_none()
    if draft is None:
        raise HTTPException(
            status_code=404,
            detail="No bundle found for this session — run /analyze first",
        )

    return json.loads(draft.bundle_json)
