"""REST endpoints for the e-commerce shopping agent."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.ecommerce import agent
from backend.ecommerce.browser_commands import BrowserExec
from backend.ecommerce.models import EcommerceAgentRun
from backend.ecommerce.schemas import (
    AgentEventOut,
    CancelRequest,
    CandidateOut,
    CheckpointOut,
    ConfirmRequest,
    RunDetailOut,
    RunOut,
    SelectCandidateRequest,
    SiteProfileOut,
    StartRunRequest,
)
from backend.ecommerce.site_catalog import all_profiles, get_profile
from backend.ecommerce.workflows import all_workflows, get_workflow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ecommerce-agent", tags=["ecommerce-agent"])


# ---------------------------------------------------------------------------
# Browser executor injection
# ---------------------------------------------------------------------------
#
# The default executor delegates to the shared browser command bridge. Tests
# can override `_executor_factory` to substitute a mock that records the
# commands the agent issued without touching a real browser.


def _default_executor() -> BrowserExec:
    from backend.elicitation.browser_bridge import execute_command

    async def _exec(command_type: str, params: dict) -> dict:
        return await execute_command(command_type, params)

    return _exec


_executor_factory: Callable[[], BrowserExec] = _default_executor


def set_executor_factory(factory: Callable[[], BrowserExec]) -> None:
    """Override the executor factory (used in tests)."""
    global _executor_factory
    _executor_factory = factory


def reset_executor_factory() -> None:
    """Reset the executor factory back to the bridge default."""
    global _executor_factory
    _executor_factory = _default_executor


def _get_executor() -> BrowserExec:
    return _executor_factory()


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _checkpoint_out(run: EcommerceAgentRun) -> CheckpointOut | None:
    cp = agent.get_checkpoint(run)
    if cp is None:
        return None
    return CheckpointOut(
        action=cp.action,
        label=cp.label,
        detail=cp.detail,
        candidate_id=cp.candidate_id,
        metadata=cp.metadata or {},
    )


def _safe_float(value: str) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _run_out(run: EcommerceAgentRun) -> RunOut:
    allowed = _decode_list(run.allowed_side_effects_json)
    forbidden = _decode_list(run.forbidden_side_effects_json)
    return RunOut(
        id=run.id,
        status=run.status,
        site_slug=run.site_slug,
        query=run.query,
        region=run.region,
        currency=run.currency,
        max_price=_safe_float(run.max_price),
        min_rating=_safe_float(run.min_rating),
        shipping_preference=run.shipping_preference,
        notes=run.notes,
        allowed_side_effects=allowed,
        forbidden_side_effects=forbidden,
        selected_candidate_id=run.selected_candidate_id,
        pending_checkpoint=_checkpoint_out(run),
        failure_reason=run.failure_reason,
        created_at=run.created_at,
        updated_at=run.updated_at,
        completed_at=run.completed_at,
    )


def _decode_list(json_text: str) -> list[str]:
    try:
        value = json.loads(json_text or "[]")
        if isinstance(value, list):
            return [str(item) for item in value]
    except json.JSONDecodeError:
        pass
    return []


async def _resolve_run(run_id: str, db: AsyncSession) -> EcommerceAgentRun:
    run = await agent.get_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")
    return run


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/sites", response_model=list[SiteProfileOut])
async def list_sites() -> list[SiteProfileOut]:
    """Return the full top-20 site catalog."""
    return [
        SiteProfileOut(
            slug=p.slug,
            name=p.name,
            domains=list(p.domains),
            regional_domains=list(p.regional_domains),
            home_url=p.home_url,
            currency=p.currency,
            locale=p.locale,
            forbidden_action_labels=list(p.forbidden_action_labels),
            notes=p.notes,
        )
        for p in all_profiles()
    ]


@router.get("/workflows")
async def list_workflows() -> list[dict[str, Any]]:
    """Return the per-site workflow manifests for the full catalog."""
    return [
        {
            "slug": w.slug,
            "notes": w.notes,
            "extra_caveats": list(w.extra_caveats),
            "mcp_server": w.mcp_server,
            "skill": w.skill,
            "stages": [
                {
                    "name": s.name,
                    "description": s.description,
                    "expected_url_substrings": list(s.expected_url_substrings),
                    "success_indicators": list(s.success_indicators),
                    "forbidden_in_stage": list(s.forbidden_in_stage),
                }
                for s in w.stages
            ],
        }
        for w in all_workflows()
    ]


@router.get("/workflows/{slug}")
async def get_workflow_endpoint(slug: str) -> dict[str, Any]:
    try:
        w = get_workflow(slug)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "slug": w.slug,
        "notes": w.notes,
        "extra_caveats": list(w.extra_caveats),
        "mcp_server": w.mcp_server,
        "skill": w.skill,
        "stages": [
            {
                "name": s.name,
                "description": s.description,
                "expected_url_substrings": list(s.expected_url_substrings),
                "success_indicators": list(s.success_indicators),
                "forbidden_in_stage": list(s.forbidden_in_stage),
            }
            for s in w.stages
        ],
    }


@router.get("/sites/{slug}", response_model=SiteProfileOut)
async def get_site(slug: str) -> SiteProfileOut:
    try:
        p = get_profile(slug)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return SiteProfileOut(
        slug=p.slug,
        name=p.name,
        domains=list(p.domains),
        regional_domains=list(p.regional_domains),
        home_url=p.home_url,
        currency=p.currency,
        locale=p.locale,
        forbidden_action_labels=list(p.forbidden_action_labels),
        notes=p.notes,
    )


@router.post("/runs", response_model=RunOut, status_code=201)
async def start_run(
    body: StartRunRequest,
    db: AsyncSession = Depends(get_db),
) -> RunOut:
    """Create a new shopping run and immediately drive the initial search."""
    try:
        run = await agent.create_run(
            db,
            site_slug=body.site_slug,
            query=body.query,
            region=body.region,
            currency=body.currency,
            max_price=body.max_price,
            min_rating=body.min_rating,
            shipping_preference=body.shipping_preference,
            notes=body.notes,
            allowed_side_effects=body.allowed_side_effects,
            forbidden_side_effects=body.forbidden_side_effects,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        executor = _get_executor()
        run = await agent.search(db, run, executor)
        if body.auto_pilot:
            run = await agent.auto_advance_after_search(db, run, executor)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Browser bridge timed out: {exc}. Is Chrome running with the "
                "NoUI extension connected?"
            ),
        ) from exc
    except Exception as exc:  # pragma: no cover — defensive surface for the user
        logger.exception("search failed")
        raise HTTPException(status_code=500, detail=f"Search failed: {exc}") from exc

    return _run_out(run)


@router.get("/runs", response_model=list[RunOut])
async def list_runs(db: AsyncSession = Depends(get_db)) -> list[RunOut]:
    from sqlalchemy import select

    result = await db.execute(
        select(EcommerceAgentRun).order_by(EcommerceAgentRun.created_at.desc())
    )
    return [_run_out(r) for r in result.scalars().all()]


@router.get("/runs/{run_id}", response_model=RunDetailOut)
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)) -> RunDetailOut:
    run = await _resolve_run(run_id, db)
    candidates = await agent.list_candidates(db, run_id)
    events = await agent.list_events(db, run_id)

    base = _run_out(run).model_dump()
    base["candidates"] = [
        CandidateOut(
            id=c.id,
            rank=c.rank,
            title=c.title,
            url=c.url,
            image_url=c.image_url,
            price_text=c.price_text,
            price_amount=_safe_float(c.price_amount),
            currency=c.currency,
            rating_text=c.rating_text,
            rating_value=_safe_float(c.rating_value),
            review_count=_safe_int(c.review_count),
            seller=c.seller,
            shipping_text=c.shipping_text,
            availability=c.availability,
            score=_safe_float(c.score),
        )
        for c in candidates
    ]
    base["events"] = [
        AgentEventOut(
            id=e.id,
            event_type=e.event_type,
            severity=e.severity,
            summary=e.summary,
            payload=_decode_payload(e.payload_json),
            timestamp=e.timestamp,
        )
        for e in events
    ]
    return RunDetailOut(**base)


def _decode_payload(text: str) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {"value": value}
    except json.JSONDecodeError:
        return {"raw": text}


@router.post("/runs/{run_id}/select", response_model=RunOut)
async def select(
    run_id: str,
    body: SelectCandidateRequest,
    db: AsyncSession = Depends(get_db),
) -> RunOut:
    run = await _resolve_run(run_id, db)
    if run.status not in {
        agent.RunStatus.AWAITING_SELECTION,
        agent.RunStatus.SELECTED,
    }:
        raise HTTPException(
            status_code=409,
            detail=f"Run status {run.status!r} does not allow selection",
        )
    try:
        run, _ = await agent.select_candidate(
            db,
            run,
            _get_executor(),
            body.candidate_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _run_out(run)


@router.post("/runs/{run_id}/confirm", response_model=RunOut)
async def confirm(
    run_id: str,
    body: ConfirmRequest,
    db: AsyncSession = Depends(get_db),
) -> RunOut:
    run = await _resolve_run(run_id, db)
    try:
        run = await agent.confirm(
            db,
            run,
            _get_executor(),
            action=body.action,
            approved=body.approved,
            user_note=body.user_note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _run_out(run)


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
async def cancel(
    run_id: str,
    body: CancelRequest,
    db: AsyncSession = Depends(get_db),
) -> RunOut:
    run = await _resolve_run(run_id, db)
    run = await agent.cancel_run(db, run, reason=body.reason)
    return _run_out(run)
