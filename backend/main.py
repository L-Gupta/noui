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

from backend.config import settings
from backend.database import Base, engine

# Import sub-module models so their tables are registered on Base.metadata
import backend.login.models  # noqa: F401
import backend.workflow.models  # noqa: F401

# Routers
from backend.login.router import router as login_router
from backend.shared.routers.clicks import router as clicks_router
from backend.shared.routers.har import router as har_router
from backend.shared.routers.url_events import router as url_events_router
from backend.workflow.router import router as workflow_router

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
    logger.info("Data directory: %s", data_dir)

    # Create all DB tables (idempotent)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured at: %s", settings.db_url)

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
# Allow all origins so curl/httpx tests also work during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Shared routers ────────────────────────────────────────────────────────────
app.include_router(clicks_router)
app.include_router(url_events_router)
app.include_router(har_router)

# ── Domain-specific routers ───────────────────────────────────────────────────
app.include_router(login_router, prefix="/login-sessions")
app.include_router(workflow_router, prefix="/workflow-sessions")


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": "1.0.0",
        "port": settings.port,
    }
