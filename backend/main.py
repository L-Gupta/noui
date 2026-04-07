"""NoUI backend — FastAPI application entry point.

Run with:
    cd /home/gabriel/Documents/adopt/noui
    uvicorn backend.main:app --port 8002 --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sqlalchemy import text

from backend.config import settings
from backend.database import Base, engine

# ── Register all ORM models so their tables are created on startup ────────────
import backend.login.models  # noqa: F401
import backend.workflow.models  # noqa: F401
import backend.elicitation.models  # noqa: F401

# ── NoUI-specific routers ─────────────────────────────────────────────────────
from backend.login.router import router as login_router
from backend.workflow.router import router as workflow_router

# ── Elicitation (ABCD) routers ────────────────────────────────────────────────
from backend.elicitation.routers.projects import router as projects_router
from backend.elicitation.routers.processes import router as processes_router
from backend.elicitation.routers.capture_sessions import router as capture_sessions_router
from backend.elicitation.routers.screenshots import router as screenshots_router
from backend.elicitation.routers.narrations import router as narrations_router
from backend.elicitation.routers.timeline import router as timeline_router
from backend.elicitation.routers.messages import router as messages_router
from backend.elicitation.routers.chat import router as chat_router
from backend.elicitation.routers.browser_commands import router as browser_commands_router
from backend.elicitation.routers.documents import router as documents_router
from backend.elicitation.routers.attachments import router as attachments_router
from backend.elicitation.routers.questions import router as questions_router
from backend.elicitation.routers.export import router as export_router
from backend.elicitation.routers.clicks import router as elicitation_clicks_router
from backend.elicitation.routers.url_events import router as elicitation_url_events_router

# ── Shared routers (HAR upload only — clicks/url_events handled by elicitation) ──
from backend.shared.routers.har import router as har_router

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Create data directories
    data_dir = Path(settings.data_dir)
    (data_dir / "har").mkdir(parents=True, exist_ok=True)
    (data_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    (data_dir / "attachments").mkdir(parents=True, exist_ok=True)
    logger.info("Data directory: %s", data_dir)

    # Create all DB tables (idempotent)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured at: %s", settings.db_url)

    # Migrate existing tables — add new columns (SQLite no-op if they already exist)
    async with engine.begin() as conn:
        for col_def in [
            "ALTER TABLE login_sessions ADD COLUMN project_id VARCHAR(36)",
            "ALTER TABLE login_sessions ADD COLUMN process_id VARCHAR(36)",
            "ALTER TABLE workflow_sessions ADD COLUMN project_id VARCHAR(36)",
            "ALTER TABLE workflow_sessions ADD COLUMN process_id VARCHAR(36)",
        ]:
            try:
                await conn.execute(text(col_def))
            except Exception:
                pass  # column already exists

    yield

    await engine.dispose()
    logger.info("NoUI backend shut down")


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="NoUI backend service for capturing browser sessions from a Chrome extension.",
    lifespan=lifespan,
)

# CORS — Chrome extensions send requests from chrome-extension:// origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Shared routers ────────────────────────────────────────────────────────────
app.include_router(har_router)

# ── Elicitation clicks + url_events (full-featured versions) ─────────────────
app.include_router(elicitation_clicks_router)
app.include_router(elicitation_url_events_router)

# ── NoUI domain routers ───────────────────────────────────────────────────────
app.include_router(login_router, prefix="/login-sessions")
app.include_router(workflow_router, prefix="/workflow-sessions")

# ── Elicitation (ABCD) routers ────────────────────────────────────────────────
app.include_router(projects_router)
app.include_router(processes_router)
app.include_router(capture_sessions_router)
app.include_router(screenshots_router)
app.include_router(narrations_router)
app.include_router(timeline_router)
app.include_router(messages_router)
app.include_router(chat_router)
app.include_router(browser_commands_router)
app.include_router(documents_router)
app.include_router(attachments_router)
app.include_router(questions_router)
app.include_router(export_router)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": "1.0.0",
        "port": settings.port,
    }
