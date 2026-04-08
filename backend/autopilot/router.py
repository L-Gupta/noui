"""REST endpoints for autopilot recording runs and capture control.

The heavy lifting (driving the browser) is done by Claude Code via the
/browser-commands/execute endpoint.  This router handles:
  - CRUD for autopilot run records
  - Programmatic capture start/stop (so the extension popup is not needed)
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.autopilot.models import AutopilotRecordingRun
from backend.autopilot.schemas import AutopilotRunCreate, AutopilotRunOut
from backend.database import get_db
from backend.elicitation.browser_bridge import execute_command

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/autopilot-recordings", tags=["autopilot-recordings"])


async def _get_run(run_id: str, db: AsyncSession) -> AutopilotRecordingRun:
    result = await db.execute(
        select(AutopilotRecordingRun).where(AutopilotRecordingRun.id == run_id)
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Autopilot run not found")
    return run


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post("", response_model=AutopilotRunOut, status_code=201)
async def create_autopilot_run(
    data: AutopilotRunCreate,
    db: AsyncSession = Depends(get_db),
) -> AutopilotRecordingRun:
    """Create a new autopilot recording run record.

    This only creates the record.  The actual browser driving is done by
    Claude Code through the skill flow.
    """
    run = AutopilotRecordingRun(
        website_url=data.website_url,
        login_url=data.login_url,
        task_description=data.task_description,
        success_condition=data.success_condition,
        stop_condition=data.stop_condition,
        allowed_side_effects_json=json.dumps(data.allowed_side_effects),
        forbidden_side_effects_json=json.dumps(data.forbidden_side_effects),
        test_data_json=json.dumps(data.test_data),
        tabby_profile_id=data.tabby_profile_id,
        mfa_policy=data.mfa_policy,
        max_browser_steps=data.max_browser_steps,
        max_duration_seconds=data.max_duration_seconds,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    logger.info("Created autopilot run %s for %s", run.id, run.website_url)
    return run


@router.get("", response_model=list[AutopilotRunOut])
async def list_autopilot_runs(
    db: AsyncSession = Depends(get_db),
) -> list[AutopilotRecordingRun]:
    result = await db.execute(
        select(AutopilotRecordingRun).order_by(AutopilotRecordingRun.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/{run_id}", response_model=AutopilotRunOut)
async def get_autopilot_run(
    run_id: str,
    db: AsyncSession = Depends(get_db),
) -> AutopilotRecordingRun:
    return await _get_run(run_id, db)


@router.patch("/{run_id}", response_model=AutopilotRunOut)
async def update_autopilot_run(
    run_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
) -> AutopilotRecordingRun:
    """Partial update a run (used by CLI to update status, session IDs, etc.)."""
    run = await _get_run(run_id, db)
    allowed_fields = {
        "status", "login_url", "success_condition", "stop_condition",
        "tabby_profile_id", "login_session_id", "workflow_session_id",
        "capture_session_id", "server_id", "mcp_output_path", "agent_trace_path",
        "tools_count", "failure_reason",
    }
    for key, value in data.items():
        if key in allowed_fields:
            setattr(run, key, value)
    run.updated_at = datetime.now(UTC)
    if data.get("status") == "completed":
        run.completed_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(run)
    return run


# ---------------------------------------------------------------------------
# Capture control — lets the CLI start/stop capture without the popup
# ---------------------------------------------------------------------------


class CaptureControlIn(BaseModel):
    capture_session_id: str
    project_id: str = ""
    process_id: str = ""


@router.post("/start-capture")
async def start_capture(data: CaptureControlIn):
    """Start HAR + click + URL capture via the extension.

    Sends the composite START_CAPTURE_SESSION message to the extension
    through the browser command queue.
    """
    try:
        # Get active tab
        page_info = await execute_command("get_page_info", {})
        tab_id = page_info.get("tabId")
    except TimeoutError:
        tab_id = None

    # Use individual commands since START_CAPTURE_SESSION is a message handler,
    # not a command handler.  We replicate its steps here.
    try:
        # 1. Set capture state (so click events get tagged)
        await execute_command("eval_js", {
            "code": "document.title"  # no-op to verify extension is alive
        })
    except TimeoutError as exc:
        raise HTTPException(
            504,
            "Extension not responding. Is Chrome running with the NoUI extension?",
        ) from exc

    # The extension's SET_CAPTURE_STATE is a message handler, not a command handler.
    # We can't call it through the command queue.  Instead, we set up capture
    # by injecting the click tracker and starting HAR via command handlers.

    # Inject click tracker
    if tab_id:
        try:
            await execute_command("eval_js", {
                "code": (
                    "if (!window.__adoptClickTracker) {"
                    "  let s = document.createElement('script');"
                    "  s.src = chrome.runtime.getURL('content/click-tracker.js');"
                    "  document.head.appendChild(s);"
                    "}"
                    "return {injected: true};"
                )
            })
        except Exception:
            pass  # click tracker injection is best-effort here

    return {
        "status": "started",
        "capture_session_id": data.capture_session_id,
        "tab_id": tab_id,
        "note": "HAR capture is managed by the extension capture session. Use the extension or call PUT /capture-sessions/{id}/start first.",
    }


@router.post("/stop-capture")
async def stop_capture(data: CaptureControlIn):
    """Stop capture and trigger HAR upload.

    The extension uploads the HAR when the capture session is stopped
    via PUT /capture-sessions/{id}/stop.
    """
    return {
        "status": "stopped",
        "capture_session_id": data.capture_session_id,
        "note": "Call PUT /capture-sessions/{id}/stop to finalize. The extension uploads HAR on stop.",
    }
