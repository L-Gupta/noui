"""End-to-end-ish tests for the e-commerce agent run state machine.

Uses an in-memory SQLite database (mirrors the production setup) and a
fully mocked browser executor, so the tests exercise the entire flow
without touching a real browser.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_NOUI_ROOT = Path(__file__).resolve().parent.parent
if str(_NOUI_ROOT) not in sys.path:
    sys.path.insert(0, str(_NOUI_ROOT))

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.ecommerce.models  # noqa: F401

# Importing Base also imports every ORM model side-effect: ensure ecommerce
# models are loaded so the table is created.
from backend.database import Base
from backend.ecommerce import agent
from backend.ecommerce.agent import RunStatus


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures (manual, since the suite doesn't use pytest-asyncio)
# ---------------------------------------------------------------------------


class FakeExecutor:
    """Browser executor stub that returns scripted responses.

    Responses can be:
      - dict: returned as-is
      - callable: receives (command_type, params) and returns a dict
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._handlers: dict[str, object] = {}

    def stub(self, command_type: str, response):
        self._handlers[command_type] = response

    def stub_eval_js_result(self, result_payload: dict) -> None:
        """Stub the next eval_js call to return ``result_payload`` as the JS result."""
        self._handlers.setdefault("_eval_js_queue", []).append(result_payload)

    async def __call__(self, command_type: str, params: dict) -> dict:
        self.calls.append((command_type, params))
        if command_type == "eval_js":
            queue = self._handlers.get("_eval_js_queue") or []
            if queue:
                payload = queue.pop(0)
                return {
                    "success": True,
                    "data": {"success": True, "result": payload},
                    "error": None,
                }
            return {"success": True, "data": {"success": True, "result": {}}, "error": None}

        handler = self._handlers.get(command_type)
        if callable(handler):
            return handler(command_type, params)  # type: ignore[no-any-return]
        if handler is not None:
            return handler  # type: ignore[return-value]
        return {"success": True, "data": {}, "error": None}


async def _make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    return engine, Session


def _candidate_payload(title: str, url: str, price: str = "$9.99") -> dict:
    return {
        "title": title,
        "url": url,
        "image_url": "",
        "price_text": price,
        "rating_text": "4.5 out of 5 stars",
        "review_count_text": "100 reviews",
        "seller": "",
        "shipping_text": "Free shipping",
        "availability": "In stock",
    }


# ---------------------------------------------------------------------------
# create_run + validation
# ---------------------------------------------------------------------------


def test_create_run_rejects_unknown_slug():
    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                with pytest.raises(ValueError):
                    await agent.create_run(db, site_slug="not-real", query="x")
        finally:
            await engine.dispose()

    _run(_go())


def test_create_run_rejects_empty_query():
    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                with pytest.raises(ValueError):
                    await agent.create_run(db, site_slug="amazon", query="   ")
        finally:
            await engine.dispose()

    _run(_go())


def test_create_run_initial_state():
    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                run = await agent.create_run(db, site_slug="amazon", query="usb cable")
            assert run.status == RunStatus.CREATED
            assert run.site_slug == "amazon"
            assert run.query == "usb cable"
            # Default policy strips place_order even if a buggy client sent it.
            assert '"place_order"' in run.forbidden_side_effects_json
        finally:
            await engine.dispose()

    _run(_go())


# ---------------------------------------------------------------------------
# search → awaiting_selection
# ---------------------------------------------------------------------------


def test_search_extracts_and_ranks_candidates():
    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                run = await agent.create_run(
                    db,
                    site_slug="amazon",
                    query="usb-c cable",
                    max_price=20.0,
                    min_rating=4.0,
                )

                executor = FakeExecutor()
                executor.stub_eval_js_result(
                    {
                        "candidates": [
                            _candidate_payload("Cable A", "https://a.example.com", "$15"),
                            _candidate_payload("Cable B", "https://b.example.com", "$8"),
                        ],
                        "total_cards": 2,
                    }
                )

                run = await agent.search(db, run, executor)

                # First call is navigate; subsequent calls include the
                # page-ready wait sequence (wait_for_url, wait_for_selector,
                # get_page_info) before eval_js. Order: navigate must come
                # first and an eval_js call must occur for extraction.
                assert executor.calls[0][0] == "navigate"
                assert executor.calls[0][1]["url"].startswith("https://www.amazon.com/s?k=")
                command_types = [c[0] for c in executor.calls]
                assert "wait_for_url" in command_types
                assert "wait_for_selector" in command_types
                assert "eval_js" in command_types

                assert run.status == RunStatus.AWAITING_SELECTION
                candidates = await agent.list_candidates(db, run.id)
                assert len(candidates) == 2
                # Cheaper item ranks higher (price-fit dominates with same rating).
                assert candidates[0].title == "Cable B"
                checkpoint = agent.get_checkpoint(run)
                assert checkpoint is not None and checkpoint.action == "select_product"
        finally:
            await engine.dispose()

    _run(_go())


def test_search_empty_results_emits_needs_user():
    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                run = await agent.create_run(db, site_slug="ebay", query="nothing")

                executor = FakeExecutor()
                # Profile extractor returns 0 cards.
                executor.stub_eval_js_result({"candidates": [], "total_cards": 0})
                # Generic fallback also returns 0 cards.
                executor.stub_eval_js_result({"candidates": [], "total_cards": 0, "source": "generic"})

                run = await agent.search(db, run, executor)
                assert run.status == RunStatus.NEEDS_USER
                assert agent.get_checkpoint(run) is not None
        finally:
            await engine.dispose()

    _run(_go())


def test_search_records_page_ready_event_before_extraction():
    """The agent must wait for the page to load before scraping."""

    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                run = await agent.create_run(db, site_slug="ebay", query="cable")

                executor = FakeExecutor()
                executor.stub_eval_js_result(
                    {
                        "candidates": [
                            _candidate_payload("Cable", "https://ebay.com/itm/1", "$8")
                        ],
                        "total_cards": 1,
                    }
                )
                run = await agent.search(db, run, executor)

                # page_ready event must appear before search_ranked.
                events = await agent.list_events(db, run.id)
                types = [e.event_type for e in events]
                assert "page_ready" in types
                assert "search_ranked" in types
                assert types.index("page_ready") < types.index("search_ranked")
                # The page_ready event must come AFTER search_start.
                assert types.index("search_start") < types.index("page_ready")
        finally:
            await engine.dispose()

    _run(_go())


def test_search_empty_surfaces_bot_check_hint_in_checkpoint():
    """When wait_for_page_ready hits a captcha URL, the checkpoint must say so."""

    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                run = await agent.create_run(db, site_slug="amazon", query="anything")

                executor = FakeExecutor()
                # Simulate amazon redirecting to a captcha page: wait_for_url
                # times out, wait_for_selector finds nothing, get_page_info
                # reports a captcha URL/title.
                executor.stub(
                    "wait_for_url",
                    {
                        "success": True,
                        "data": {
                            "matched": False,
                            "url": "https://www.amazon.com/errors/validateCaptcha?args=...",
                            "waited": 6000,
                            "timeout": True,
                        },
                    },
                )
                executor.stub(
                    "wait_for_selector",
                    {
                        "success": True,
                        "data": {"found": False, "waited": 1000, "timeout": True},
                    },
                )
                executor.stub(
                    "get_page_info",
                    {
                        "success": True,
                        "data": {
                            "url": "https://www.amazon.com/errors/validateCaptcha?args=...",
                            "title": "Robot Check",
                        },
                    },
                )
                # Both extractor passes return zero cards.
                executor.stub_eval_js_result({"candidates": [], "total_cards": 0})
                executor.stub_eval_js_result({"candidates": [], "total_cards": 0, "source": "generic"})

                run = await agent.search(db, run, executor)
                assert run.status == RunStatus.NEEDS_USER
                cp = agent.get_checkpoint(run)
                assert cp is not None
                assert cp.metadata.get("bot_check_hint") is True
                # The detail message must explicitly mention captcha/bot-check.
                assert "captcha" in cp.detail.lower() or "bot" in cp.detail.lower()

                # The search_empty audit payload also surfaces the diagnostic.
                events = await agent.list_events(db, run.id)
                empty_evts = [e for e in events if e.event_type == "search_empty"]
                assert empty_evts
                import json as _json

                payload = _json.loads(empty_evts[-1].payload_json)
                assert payload["bot_check_hint"] is True
                assert "captcha" in payload["final_url"].lower()
        finally:
            await engine.dispose()

    _run(_go())


def test_add_to_cart_signin_wall_triggers_retry_checkpoint():
    """When no Add-to-cart button is found AND the page is a sign-in wall,
    the agent must surface a retry_add_to_cart checkpoint, not a generic
    failure."""

    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                run = await agent.create_run(db, site_slug="amazon", query="x")
                executor = FakeExecutor()
                executor.stub_eval_js_result(
                    {
                        "candidates": [
                            _candidate_payload("Cable", "https://a.example.com", "$10")
                        ],
                        "total_cards": 1,
                    }
                )
                run = await agent.search(db, run, executor)
                candidates = await agent.list_candidates(db, run.id)
                executor.stub_eval_js_result({"highlighted": True})
                run, _ = await agent.select_candidate(db, run, executor, candidates[0].id)

                # detect_purchase_boundary on product page — none present.
                executor.stub_eval_js_result({"boundary_present": False, "matches": []})
                # click_by_text never clicks (no add-to-cart button found).
                executor.stub("click_by_text", {"success": True, "data": {"clicked": False}, "error": None})
                # detect_signin_wall — reports a clear sign-in wall.
                executor.stub_eval_js_result(
                    {
                        "signin_present": True,
                        "signin_controls": 4,
                        "addtocart_controls": 0,
                        "has_password_field": True,
                        "title_hint": True,
                        "hits": ["sign in"],
                        "url": "https://www.amazon.com/ap/signin",
                    }
                )

                run = await agent.confirm(
                    db, run, executor, action="add_to_cart", approved=True
                )

                assert run.status == RunStatus.NEEDS_USER
                cp = agent.get_checkpoint(run)
                assert cp is not None
                assert cp.action == "retry_add_to_cart"
                assert "sign in" in cp.label.lower()

                events = await agent.list_events(db, run.id)
                assert any(e.event_type == "signin_wall_detected" for e in events)
        finally:
            await engine.dispose()

    _run(_go())


def test_confirm_retry_add_to_cart_reruns_add_to_cart():
    """After the user signs in and approves retry_add_to_cart, the agent
    must re-attempt the add and proceed to awaiting_checkout_confirmation."""

    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                run = await agent.create_run(db, site_slug="amazon", query="x")
                executor = FakeExecutor()
                executor.stub_eval_js_result(
                    {
                        "candidates": [
                            _candidate_payload("Cable", "https://a.example.com", "$10")
                        ],
                        "total_cards": 1,
                    }
                )
                run = await agent.search(db, run, executor)
                candidates = await agent.list_candidates(db, run.id)
                executor.stub_eval_js_result({"highlighted": True})
                run, _ = await agent.select_candidate(db, run, executor, candidates[0].id)

                # First add-to-cart attempt: blocked by sign-in wall.
                executor.stub_eval_js_result({"boundary_present": False, "matches": []})
                executor.stub("click_by_text", {"success": True, "data": {"clicked": False}, "error": None})
                executor.stub_eval_js_result(
                    {
                        "signin_present": True,
                        "signin_controls": 4,
                        "addtocart_controls": 0,
                        "has_password_field": True,
                        "title_hint": True,
                        "hits": ["sign in"],
                    }
                )
                run = await agent.confirm(
                    db, run, executor, action="add_to_cart", approved=True
                )
                assert run.status == RunStatus.NEEDS_USER
                cp = agent.get_checkpoint(run)
                assert cp is not None and cp.action == "retry_add_to_cart"

                # User signs in and approves retry. This time click succeeds.
                executor.stub_eval_js_result({"boundary_present": False, "matches": []})
                executor.stub("click_by_text", {"success": True, "data": {"clicked": True}, "error": None})
                executor.stub_eval_js_result(
                    {
                        "line_count": 1,
                        "lines": [{"index": 0, "title": "Cable"}],
                        "subtotal_text": "$10.00",
                        "buttons": [{"label": "Proceed to checkout"}],
                        "url": "https://www.amazon.com/cart",
                    }
                )

                run = await agent.confirm(
                    db, run, executor, action="retry_add_to_cart", approved=True
                )
                assert run.status == RunStatus.AWAITING_CHECKOUT_CONFIRMATION
                cp = agent.get_checkpoint(run)
                assert cp is not None and cp.action == "proceed_to_checkout"
                # failure_reason cleared after retry.
                assert run.failure_reason == ""
        finally:
            await engine.dispose()

    _run(_go())


# ---------------------------------------------------------------------------
# select → awaiting_cart_confirmation
# ---------------------------------------------------------------------------


def test_select_candidate_transitions_to_awaiting_cart_confirmation():
    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                run = await agent.create_run(db, site_slug="amazon", query="x")
                executor = FakeExecutor()
                executor.stub_eval_js_result(
                    {
                        "candidates": [
                            _candidate_payload("Cable A", "https://a.example.com", "$10")
                        ],
                        "total_cards": 1,
                    }
                )
                run = await agent.search(db, run, executor)
                candidates = await agent.list_candidates(db, run.id)
                assert candidates

                # Stub highlight_candidate eval_js
                executor.stub_eval_js_result({"highlighted": True})

                run, checkpoint = await agent.select_candidate(
                    db, run, executor, candidates[0].id
                )
                assert run.status == RunStatus.AWAITING_CART_CONFIRMATION
                assert run.selected_candidate_id == candidates[0].id
                assert checkpoint.action == "add_to_cart"
                assert checkpoint.candidate_id == candidates[0].id
        finally:
            await engine.dispose()

    _run(_go())


def test_select_candidate_unknown_id_raises():
    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                run = await agent.create_run(db, site_slug="amazon", query="x")
                executor = FakeExecutor()
                executor.stub_eval_js_result({"candidates": [], "total_cards": 0})
                run = await agent.search(db, run, executor)
                with pytest.raises(ValueError):
                    await agent.select_candidate(
                        db, run, executor, "00000000-0000-0000-0000-000000000000"
                    )
        finally:
            await engine.dispose()

    _run(_go())


# ---------------------------------------------------------------------------
# confirm → add_to_cart → in_cart → awaiting_checkout_confirmation
# ---------------------------------------------------------------------------


def test_confirm_add_to_cart_runs_add_then_pauses_for_checkout():
    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                run = await agent.create_run(db, site_slug="amazon", query="x")
                executor = FakeExecutor()
                executor.stub_eval_js_result(
                    {
                        "candidates": [
                            _candidate_payload("Cable", "https://a.example.com", "$10")
                        ],
                        "total_cards": 1,
                    }
                )
                run = await agent.search(db, run, executor)
                candidates = await agent.list_candidates(db, run.id)

                executor.stub_eval_js_result({"highlighted": True})
                run, _ = await agent.select_candidate(db, run, executor, candidates[0].id)

                # Stub the eval_js calls that happen during add-to-cart:
                # 1. detect_purchase_boundary (none present)
                # 2. extract_cart_summary
                executor.stub_eval_js_result({"boundary_present": False, "matches": []})
                executor.stub_eval_js_result(
                    {
                        "line_count": 1,
                        "lines": [{"index": 0, "title": "Cable"}],
                        "subtotal_text": "$10.00",
                        "buttons": [{"label": "Proceed to checkout"}],
                        "url": "https://www.amazon.com/cart",
                    }
                )
                # Stub click_by_text → first labels succeed
                executor.stub("click_by_text", {"success": True, "data": {"clicked": True}, "error": None})

                run = await agent.confirm(
                    db, run, executor, action="add_to_cart", approved=True
                )

                assert run.status == RunStatus.AWAITING_CHECKOUT_CONFIRMATION
                cp = agent.get_checkpoint(run)
                assert cp is not None and cp.action == "proceed_to_checkout"
        finally:
            await engine.dispose()

    _run(_go())


def test_confirm_with_decline_cancels_run():
    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                run = await agent.create_run(db, site_slug="amazon", query="x")
                executor = FakeExecutor()
                executor.stub_eval_js_result(
                    {
                        "candidates": [
                            _candidate_payload("Cable", "https://a.example.com", "$10")
                        ],
                        "total_cards": 1,
                    }
                )
                run = await agent.search(db, run, executor)
                candidates = await agent.list_candidates(db, run.id)
                executor.stub_eval_js_result({"highlighted": True})
                run, _ = await agent.select_candidate(db, run, executor, candidates[0].id)

                run = await agent.confirm(
                    db,
                    run,
                    executor,
                    action="add_to_cart",
                    approved=False,
                    user_note="changed my mind",
                )
                assert run.status == RunStatus.CANCELLED
        finally:
            await engine.dispose()

    _run(_go())


def test_confirm_hard_forbidden_action_is_rejected_even_when_approved():
    """Approving 'place_order' must NOT execute it — policy is the final word."""

    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                run = await agent.create_run(db, site_slug="amazon", query="x")
                executor = FakeExecutor()
                executor.stub_eval_js_result(
                    {
                        "candidates": [
                            _candidate_payload("Cable", "https://a.example.com", "$10")
                        ],
                        "total_cards": 1,
                    }
                )
                run = await agent.search(db, run, executor)
                candidates = await agent.list_candidates(db, run.id)
                executor.stub_eval_js_result({"highlighted": True})
                run, _ = await agent.select_candidate(db, run, executor, candidates[0].id)

                # Force a forbidden checkpoint so we can test the gate.
                import json

                run.pending_checkpoint_json = json.dumps(
                    {
                        "action": "place_order",
                        "label": "Place your order",
                        "detail": "test",
                        "candidate_id": None,
                        "metadata": {},
                    }
                )

                approved_calls_before = len(executor.calls)
                run = await agent.confirm(
                    db, run, executor, action="place_order", approved=True
                )
                # No browser commands should be dispatched after policy blocks.
                assert len(executor.calls) == approved_calls_before
                assert run.status == RunStatus.NEEDS_USER
                # Audit log records a policy_blocked event.
                events = await agent.list_events(db, run.id)
                assert any(e.event_type == "policy_blocked" for e in events)
        finally:
            await engine.dispose()

    _run(_go())


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_audit_log_records_full_lifecycle():
    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                run = await agent.create_run(db, site_slug="amazon", query="x")
                executor = FakeExecutor()
                executor.stub_eval_js_result(
                    {
                        "candidates": [
                            _candidate_payload("Cable", "https://a.example.com", "$10")
                        ],
                        "total_cards": 1,
                    }
                )
                run = await agent.search(db, run, executor)
                candidates = await agent.list_candidates(db, run.id)
                executor.stub_eval_js_result({"highlighted": True})
                run, _ = await agent.select_candidate(db, run, executor, candidates[0].id)

                events = await agent.list_events(db, run.id)
                types = [e.event_type for e in events]
                assert "run_created" in types
                assert "search_start" in types
                assert "search_ranked" in types
                assert "candidate_selected" in types
        finally:
            await engine.dispose()

    _run(_go())


# ---------------------------------------------------------------------------
# cancel_run
# ---------------------------------------------------------------------------


def test_auto_advance_after_search_picks_top_and_adds_to_cart():
    """The full auto-pilot path: search → auto-select → auto-add → pause at checkout."""

    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                run = await agent.create_run(db, site_slug="amazon", query="usb cable")

                executor = FakeExecutor()
                # 1. search → eval_js extract_product_cards
                executor.stub_eval_js_result(
                    {
                        "candidates": [
                            _candidate_payload("Premium Cable", "https://a.example.com/1", "$8"),
                            _candidate_payload("Budget Cable", "https://a.example.com/2", "$15"),
                        ],
                        "total_cards": 2,
                    }
                )
                # 2. auto_advance → select → highlight_candidate eval_js
                executor.stub_eval_js_result({"highlighted": True})
                # 3. add_to_cart → detect_purchase_boundary eval_js
                executor.stub_eval_js_result({"boundary_present": False, "matches": []})
                # 4. add_to_cart → extract_cart_summary eval_js
                executor.stub_eval_js_result(
                    {
                        "line_count": 1,
                        "lines": [{"index": 0, "title": "Premium Cable"}],
                        "subtotal_text": "$8.00",
                        "buttons": [{"label": "Proceed to checkout"}],
                        "url": "https://www.amazon.com/cart",
                    }
                )
                executor.stub(
                    "click_by_text",
                    {"success": True, "data": {"clicked": True}, "error": None},
                )

                run = await agent.search(db, run, executor)
                assert run.status == RunStatus.AWAITING_SELECTION
                run = await agent.auto_advance_after_search(db, run, executor)

                # The single human-confirmation point: right before checkout.
                assert run.status == RunStatus.AWAITING_CHECKOUT_CONFIRMATION
                cp = agent.get_checkpoint(run)
                assert cp is not None and cp.action == "proceed_to_checkout"

                # The top-ranked candidate (cheaper one) was auto-selected.
                candidates = await agent.list_candidates(db, run.id)
                assert candidates[0].title == "Premium Cable"
                assert run.selected_candidate_id == candidates[0].id

                # Audit trail records the auto-pilot transitions.
                events = await agent.list_events(db, run.id)
                types = [e.event_type for e in events]
                assert "auto_selected" in types
                assert "auto_add_to_cart" in types
                assert "add_to_cart_click" in types
                assert "cart_summary" in types
        finally:
            await engine.dispose()

    _run(_go())


def test_auto_advance_noop_when_search_left_run_in_needs_user():
    """If search hit a captcha and the run is at needs_user, auto-advance must not run."""

    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                run = await agent.create_run(db, site_slug="amazon", query="x")
                executor = FakeExecutor()
                executor.stub_eval_js_result({"candidates": [], "total_cards": 0})
                run = await agent.search(db, run, executor)
                assert run.status == RunStatus.NEEDS_USER

                run = await agent.auto_advance_after_search(db, run, executor)
                assert run.status == RunStatus.NEEDS_USER
        finally:
            await engine.dispose()

    _run(_go())


def test_cancel_run_records_event_and_marks_cancelled():
    async def _go():
        engine, Session = await _make_session()
        try:
            async with Session() as db:
                run = await agent.create_run(db, site_slug="amazon", query="x")
                run = await agent.cancel_run(db, run, reason="user backed out")
                assert run.status == RunStatus.CANCELLED
                assert run.failure_reason == "user backed out"
                events = await agent.list_events(db, run.id)
                assert any(e.event_type == "run_cancelled" for e in events)
        finally:
            await engine.dispose()

    _run(_go())
