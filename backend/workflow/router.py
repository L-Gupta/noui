"""Routes for workflow-recording sessions."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.elicitation.models import Process, Project
from backend.shared.models import ClickEvent, HarFile, UrlEvent
from backend.workflow.models import WorkflowSession
from backend.workflow.schemas import (
    WorkflowSessionCreate,
    WorkflowSessionOut,
    WorkflowSessionUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["workflow-sessions"])

# noui/ root: workflow/router.py → workflow/ → backend/ → noui/
_NOUI_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _find_or_create_project(db: AsyncSession, name: str, url: str) -> str:
    """Find an existing project by URL hostname, or create one."""
    hostname = urlparse(url).hostname or name
    result = await db.execute(select(Project).where(Project.base_url.contains(hostname)))
    project = result.scalar_one_or_none()
    if not project:
        project = Project(name=hostname, base_url=url)
        db.add(project)
        await db.flush()
    return project.id


async def _get_session(session_id: str, db: AsyncSession) -> WorkflowSession:
    result = await db.execute(select(WorkflowSession).where(WorkflowSession.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Workflow session not found")
    return session


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post("", response_model=WorkflowSessionOut, status_code=201)
async def create_workflow_session(
    data: WorkflowSessionCreate,
    db: AsyncSession = Depends(get_db),
) -> WorkflowSession:
    """Create a new workflow-recording session and ensure an App + Process exist."""
    project_id = await _find_or_create_project(db, data.name, data.start_url)
    process = Process(project_id=project_id, name=data.name, base_url=data.start_url)
    db.add(process)
    await db.flush()

    session = WorkflowSession(
        name=data.name,
        start_url=data.start_url,
        description=data.description,
        project_id=project_id,
        process_id=process.id,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    logger.info(
        "Created workflow session %s: %s (project=%s)", session.id, session.name, project_id
    )
    return session


@router.get("", response_model=list[WorkflowSessionOut])
async def list_workflow_sessions(db: AsyncSession = Depends(get_db)) -> list[WorkflowSession]:
    """List all workflow sessions, newest first."""
    result = await db.execute(select(WorkflowSession).order_by(WorkflowSession.created_at.desc()))
    return list(result.scalars().all())


@router.get("/{session_id}", response_model=WorkflowSessionOut)
async def get_workflow_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> WorkflowSession:
    return await _get_session(session_id, db)


@router.patch("/{session_id}", response_model=WorkflowSessionOut)
async def update_workflow_session(
    session_id: str,
    data: WorkflowSessionUpdate,
    db: AsyncSession = Depends(get_db),
) -> WorkflowSession:
    """Partial-update a workflow session."""
    session = await _get_session(session_id, db)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(session, field, value)
    await db.commit()
    await db.refresh(session)
    return session


@router.post("/{session_id}/start", response_model=WorkflowSessionOut)
async def start_workflow_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> WorkflowSession:
    """Mark a workflow session as actively recording."""
    session = await _get_session(session_id, db)
    if session.status not in ("idle",):
        raise HTTPException(
            status_code=409, detail=f"Session is already in status '{session.status}'"
        )
    session.status = "recording"
    session.started_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(session)
    return session


@router.post("/{session_id}/complete", response_model=WorkflowSessionOut)
async def complete_workflow_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> WorkflowSession:
    """Mark a workflow session as completed."""
    session = await _get_session(session_id, db)
    session.status = "completed"
    session.completed_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(session)
    logger.info("Completed workflow session %s", session_id)
    return session


@router.delete("/{session_id}", status_code=204)
async def delete_workflow_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a workflow session and all its associated data."""
    session = await _get_session(session_id, db)
    await db.delete(session)
    await db.commit()


# ---------------------------------------------------------------------------
# Workflow export — unified (mcp | skill | both)
# ---------------------------------------------------------------------------


async def _load_compile_inputs(
    session_id: str,
    capture_session_id: str,
    db: AsyncSession,
) -> tuple[WorkflowSession, str, dict, list[dict], list[dict]]:
    """Load everything the compilers need from the database + HAR on disk.

    Returns (session, app_slug, har, click_dicts, url_dicts).
    Raises HTTPException on missing HAR or parse failure.
    """
    session = await _get_session(session_id, db)

    har_lookup_id = capture_session_id or session_id
    har_session_type = "workflow"

    click_result = await db.execute(
        select(ClickEvent)
        .where(ClickEvent.session_id == har_lookup_id, ClickEvent.session_type == har_session_type)
        .order_by(ClickEvent.timestamp)
    )
    click_rows = list(click_result.scalars().all())

    if not click_rows and capture_session_id:
        click_result = await db.execute(
            select(ClickEvent)
            .where(ClickEvent.capture_session_id == capture_session_id)
            .order_by(ClickEvent.timestamp)
        )
        click_rows = list(click_result.scalars().all())

    url_result = await db.execute(
        select(UrlEvent)
        .where(UrlEvent.session_id == har_lookup_id, UrlEvent.session_type == har_session_type)
        .order_by(UrlEvent.timestamp)
    )
    url_rows = list(url_result.scalars().all())

    har_result = await db.execute(
        select(HarFile)
        .where(HarFile.session_id == har_lookup_id)
        .order_by(HarFile.created_at.desc())
    )
    har_file = har_result.scalar_one_or_none()

    if not har_file and capture_session_id and har_lookup_id != session_id:
        har_result = await db.execute(
            select(HarFile)
            .where(HarFile.session_id == session_id)
            .order_by(HarFile.created_at.desc())
        )
        har_file = har_result.scalar_one_or_none()

    if not har_file:
        raise HTTPException(status_code=422, detail="No HAR file found for this session")

    har_path = Path(har_file.file_path)
    if not har_path.exists():
        raise HTTPException(
            status_code=422,
            detail=f"HAR file not found on disk: {har_file.file_path}",
        )
    try:
        har = json.loads(har_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=422, detail=f"Failed to read HAR file: {exc}") from exc

    def _click_to_dict(c: ClickEvent) -> dict:
        return {
            "id": c.id,
            "event_type": c.event_type,
            "url": c.url,
            "tag_name": c.tag_name,
            "field_name": c.field_name,
            "value": c.value,
            "timestamp": c.timestamp.isoformat() if c.timestamp else None,
        }

    def _url_to_dict(u: UrlEvent) -> dict:
        return {
            "id": u.id,
            "from_url": u.from_url,
            "to_url": u.to_url,
            "timestamp": u.timestamp.isoformat() if u.timestamp else None,
        }

    click_dicts = [_click_to_dict(c) for c in click_rows]
    url_dicts = [_url_to_dict(u) for u in url_rows]

    app_slug = re.sub(r"[^a-z0-9]+", "-", session.name.lower()).strip("-") or "app"

    return session, app_slug, har, click_dicts, url_dicts


@router.post("/{session_id}/export")
async def export_workflow(
    session_id: str,
    target: str = Query(
        "both",
        alias="as",
        description="Output format: 'mcp', 'skill', or 'both' (default).",
    ),
    tabby_profile_id: str = Query(
        "",
        description=(
            "Legacy: Tabby profile ID (UUID or slug). Prefer profile_slug for new integrations."
        ),
    ),
    profile_slug: str = Query(
        "",
        description=(
            "Tabby profile slug for runtime credential requests "
            "(POST /credentials/request). Takes precedence over tabby_profile_id."
        ),
    ),
    profile_db_id: str = Query(
        "",
        description="Tabby profile DB UUID for admin/version operations only.",
    ),
    description_override: str = Query(
        "",
        description="Skill-only: explicit SKILL.md description (skips the heuristic).",
    ),
    capture_session_id: str = Query(
        "",
        description="ABCD capture session ID to use instead of workflow session data",
    ),
    execution_mode: str = Query(
        "cdp",
        description=(
            "Execution strategy: 'cdp' (default — operations run inside Tabby's "
            "browser via CDP) or 'http' (legacy httpx + resolve_auth)."
        ),
    ),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Compile a workflow recording into an MCP server, a Skill, or both.

    Output roots:
      - mcp   → noui/workbench/mcp_servers/<app_slug>/<server_id>/
      - skill → noui/workbench/skills/<app_slug>/

    Returns a dict with top-level keys `mcp` and/or `skill` depending on `as`.
    Each block contains the respective manifest.
    """
    if target not in ("mcp", "skill", "both"):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid `as` value {target!r}. Expected 'mcp', 'skill', or 'both'.",
        )
    if execution_mode not in ("cdp", "http"):
        raise HTTPException(
            status_code=422,
            detail=(f"Invalid execution_mode {execution_mode!r}. Expected 'cdp' or 'http'."),
        )

    session, app_slug, har, click_dicts, url_dicts = await _load_compile_inputs(
        session_id, capture_session_id, db
    )

    result: dict = {}

    from compiler.mcp.har_to_tools import HarValidationError

    if target in ("mcp", "both"):
        server_id = f"{app_slug}-{session_id[:8]}"
        mcp_output_dir = str(_NOUI_ROOT / "workbench" / "mcp_servers" / app_slug / server_id)
        try:
            from compiler.mcp.server_generator import compile_workflow

            mcp_manifest = compile_workflow(
                session_id=session_id,
                session_name=session.name,
                app_slug=app_slug,
                tabby_profile_id=tabby_profile_id,
                har=har,
                click_events=click_dicts,
                url_events=url_dicts,
                output_dir=mcp_output_dir,
                profile_slug=profile_slug,
                profile_db_id=profile_db_id,
                execution_mode=execution_mode,
            )
        except HarValidationError as exc:
            logger.warning("MCP compilation rejected for session %s: %s", session_id, exc)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("MCP compilation failed for session %s", session_id)
            raise HTTPException(status_code=500, detail=f"MCP compilation failed: {exc}") from exc
        logger.info("Exported MCP server for session %s → %s", session_id, mcp_output_dir)
        result["mcp"] = mcp_manifest

    if target in ("skill", "both"):
        skill_output_dir = str(_NOUI_ROOT / "workbench" / "skills" / app_slug)
        try:
            from compiler.skill.skill_generator import compile_workflow_to_skill

            skill_manifest = compile_workflow_to_skill(
                session_id=session_id,
                session_name=session.name,
                app_slug=app_slug,
                tabby_profile_id=tabby_profile_id,
                har=har,
                click_events=click_dicts,
                url_events=url_dicts,
                output_dir=skill_output_dir,
                profile_slug=profile_slug,
                profile_db_id=profile_db_id,
                description_override=description_override,
                execution_mode=execution_mode,
                start_url=session.start_url or "",
            )
        except HarValidationError as exc:
            logger.warning("Skill compilation rejected for session %s: %s", session_id, exc)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Skill compilation failed for session %s", session_id)
            raise HTTPException(status_code=500, detail=f"Skill compilation failed: {exc}") from exc
        logger.info("Exported skill for session %s → %s", session_id, skill_output_dir)
        result["skill"] = skill_manifest

    return result
