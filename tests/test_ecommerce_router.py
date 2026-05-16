"""Router-level tests for the e-commerce agent.

Uses FastAPI's TestClient with the executor factory overridden so the
endpoints can be exercised end-to-end without a real browser.
"""

from __future__ import annotations

import sys
from pathlib import Path

_NOUI_ROOT = Path(__file__).resolve().parent.parent
if str(_NOUI_ROOT) not in sys.path:
    sys.path.insert(0, str(_NOUI_ROOT))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.autopilot.models  # noqa: F401
import backend.ecommerce.models  # noqa: F401
import backend.elicitation.models  # noqa: F401
import backend.login.models  # noqa: F401
import backend.workflow.models  # noqa: F401

# Load all ORM modules so create_all knows about every table.
from backend.database import (
    Base,
    get_db,  # noqa: E402
)
from backend.ecommerce import router as ecommerce_router_mod  # noqa: E402


@pytest.fixture
def app_client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    import asyncio

    asyncio.run(_init_schema(engine))

    async def _override_get_db():
        async with Session() as session:
            yield session

    # Install a fake browser executor that returns canned product cards. The
    # queue covers the full auto_pilot flow: search extract → highlight →
    # boundary detect → cart summary. Any further eval_js falls back to an
    # empty dict, which causes the agent to record the no-data response in
    # its audit log instead of crashing.
    class _FakeExec:
        def __init__(self):
            self._eval_queue = [
                {
                    "candidates": [
                        {
                            "title": "Test Cable",
                            "url": "https://www.amazon.com/dp/B000TEST",
                            "image_url": "",
                            "price_text": "$9.99",
                            "rating_text": "4.5 out of 5 stars",
                            "review_count_text": "100 reviews",
                            "seller": "",
                            "shipping_text": "",
                            "availability": "In Stock",
                        }
                    ],
                    "total_cards": 1,
                },
                {"highlighted": True},
                {"boundary_present": False, "matches": []},
                {
                    "line_count": 1,
                    "lines": [{"index": 0, "title": "Test Cable"}],
                    "subtotal_text": "$9.99",
                    "buttons": [{"label": "Proceed to checkout"}],
                    "url": "https://www.amazon.com/cart",
                },
            ]

        async def __call__(self, command_type: str, params: dict) -> dict:
            if command_type == "eval_js" and self._eval_queue:
                return {
                    "success": True,
                    "data": {"success": True, "result": self._eval_queue.pop(0)},
                    "error": None,
                }
            if command_type == "navigate":
                return {"success": True, "data": {"navigated": True}, "error": None}
            if command_type == "click_by_text":
                return {"success": True, "data": {"clicked": True}, "error": None}
            return {"success": True, "data": {}, "error": None}

    ecommerce_router_mod.set_executor_factory(lambda: _FakeExec())

    app = FastAPI()
    app.include_router(ecommerce_router_mod.router)
    app.dependency_overrides[get_db] = _override_get_db

    client = TestClient(app)
    try:
        yield client
    finally:
        ecommerce_router_mod.reset_executor_factory()
        asyncio.run(engine.dispose())


async def _init_schema(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def test_list_sites_returns_twenty(app_client):
    resp = app_client.get("/ecommerce-agent/sites")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 20
    slugs = {s["slug"] for s in body}
    for required in ("amazon", "ebay", "aliexpress", "walmart", "etsy"):
        assert required in slugs


def test_get_unknown_site_returns_404(app_client):
    resp = app_client.get("/ecommerce-agent/sites/not-real")
    assert resp.status_code == 404


def test_list_workflows_returns_all_stages_per_site(app_client):
    resp = app_client.get("/ecommerce-agent/workflows")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 20
    for entry in body:
        stages = {s["name"] for s in entry["stages"]}
        assert {"search", "browse", "cart", "checkout_prep"}.issubset(stages)


def test_start_run_auto_pilot_lands_at_checkout_confirmation(app_client):
    """Default behavior: agent runs end-to-end and pauses at the single checkout gate."""
    resp = app_client.post(
        "/ecommerce-agent/runs",
        json={"site_slug": "amazon", "query": "usb cable"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "awaiting_checkout_confirmation"
    assert body["site_slug"] == "amazon"
    assert body["pending_checkpoint"]["action"] == "proceed_to_checkout"

    detail = app_client.get(f"/ecommerce-agent/runs/{body['id']}").json()
    assert len(detail["candidates"]) == 1
    assert detail["candidates"][0]["title"] == "Test Cable"
    # The selected candidate is the top-ranked one.
    assert detail["selected_candidate_id"] == detail["candidates"][0]["id"]


def test_start_run_auto_pilot_false_preserves_manual_flow(app_client):
    """Explicit opt-out: every side effect still requires confirmation."""
    resp = app_client.post(
        "/ecommerce-agent/runs",
        json={"site_slug": "amazon", "query": "usb cable", "auto_pilot": False},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "awaiting_selection"
    assert body["pending_checkpoint"]["action"] == "select_product"


def test_start_run_rejects_unknown_site(app_client):
    resp = app_client.post(
        "/ecommerce-agent/runs",
        json={"site_slug": "made-up-store", "query": "anything"},
    )
    assert resp.status_code == 400


def test_cancel_run(app_client):
    started = app_client.post(
        "/ecommerce-agent/runs",
        json={"site_slug": "amazon", "query": "cable"},
    ).json()
    resp = app_client.post(
        f"/ecommerce-agent/runs/{started['id']}/cancel",
        json={"reason": "no longer needed"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
