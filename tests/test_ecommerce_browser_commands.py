"""Tests for the e-commerce agent's browser command wrappers.

These tests verify the JS payload assembly and result unwrapping without
talking to a real browser — `BrowserExec` is replaced with a recorder mock.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_NOUI_ROOT = Path(__file__).resolve().parent.parent
if str(_NOUI_ROOT) not in sys.path:
    sys.path.insert(0, str(_NOUI_ROOT))

import pytest

from backend.ecommerce import browser_commands as bc
from backend.ecommerce.site_catalog import get_profile


class RecordingExecutor:
    """Records every browser command issued, returns canned responses."""

    def __init__(self, responses: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._responses = list(responses or [])

    async def __call__(self, command_type: str, params: dict) -> dict:
        self.calls.append((command_type, params))
        if self._responses:
            return self._responses.pop(0)
        return {"success": True, "data": {"success": True, "result": {}}, "error": None}


def _run(coro):
    return asyncio.run(coro)


def test_eval_js_payloads_have_toplevel_return():
    """Guardrail: every eval_js payload MUST start with a top-level ``return``.

    The extension runs the code as ``new Function(body)()``. A bare IIFE
    expression statement (``(function(){...})()``) evaluates and is
    discarded; the wrapping function returns ``undefined`` and the agent
    sees zero results. This test guards against that regression.
    """
    for name in (
        "_EXTRACT_PRODUCT_CARDS_JS",
        "_EXTRACT_PRODUCT_CARDS_GENERIC_JS",
        "_EXTRACT_CART_SUMMARY_JS",
        "_DETECT_PURCHASE_BOUNDARY_JS",
        "_DETECT_SIGNIN_WALL_JS",
        "_HIGHLIGHT_CANDIDATE_JS",
    ):
        payload = getattr(bc, name)
        stripped = payload.lstrip()
        assert stripped.startswith("return "), (
            f"{name} must start with `return ` so `new Function(body)()` "
            f"propagates the IIFE result. Got: {stripped[:60]!r}"
        )


# ---------------------------------------------------------------------------
# extract_product_cards
# ---------------------------------------------------------------------------


def test_extract_product_cards_injects_selectors_into_payload():
    profile = get_profile("amazon")
    rec = RecordingExecutor(
        [
            {
                "success": True,
                "data": {
                    "success": True,
                    "result": {
                        "candidates": [
                            {
                                "title": "USB-C Cable",
                                "url": "https://www.amazon.com/dp/B000",
                                "price_text": "$9.99",
                            }
                        ],
                        "total_cards": 1,
                    },
                },
            }
        ]
    )
    result = _run(bc.extract_product_cards(rec, profile, limit=10))
    assert result["candidates"][0]["title"] == "USB-C Cable"
    assert result["total_cards"] == 1

    assert len(rec.calls) == 1
    command_type, params = rec.calls[0]
    assert command_type == "eval_js"
    code = params["code"]
    # Card selectors from the profile should be JSON-encoded into the code.
    for sel in profile.product_card_selectors:
        assert sel in code
    # Limit gets injected.
    assert "10" in code


def test_extract_product_cards_handles_empty_result():
    profile = get_profile("ebay")
    rec = RecordingExecutor(
        [{"success": True, "data": {"success": True, "result": {"candidates": []}}, "error": None}]
    )
    result = _run(bc.extract_product_cards(rec, profile, limit=5))
    assert result.get("candidates") == []


# ---------------------------------------------------------------------------
# detect_purchase_boundary
# ---------------------------------------------------------------------------


def test_detect_purchase_boundary_includes_universal_and_per_site_labels():
    profile = get_profile("amazon")
    rec = RecordingExecutor(
        [
            {
                "success": True,
                "data": {
                    "success": True,
                    "result": {
                        "url": "https://www.amazon.com/gp/buy/spc/handlers/display.html",
                        "boundary_present": True,
                        "matches": [{"label": "place your order", "tag": "input", "matched": "place your order"}],
                    },
                },
            }
        ]
    )
    result = _run(bc.detect_purchase_boundary(rec, profile))
    assert result["boundary_present"] is True

    _, params = rec.calls[0]
    code = params["code"]
    # The JS payload must contain at least one universal and one site-specific label.
    assert "place order" in code
    assert "1-click" in code or "buy now" in code


def test_detect_purchase_boundary_handles_no_profile():
    rec = RecordingExecutor(
        [
            {
                "success": True,
                "data": {"success": True, "result": {"boundary_present": False, "matches": []}},
                "error": None,
            }
        ]
    )
    result = _run(bc.detect_purchase_boundary(rec, None))
    assert result["boundary_present"] is False
    _, params = rec.calls[0]
    # JSON-encoded labels list must always include the common forbidden phrases.
    assert "place order" in params["code"]


# ---------------------------------------------------------------------------
# highlight_candidate
# ---------------------------------------------------------------------------


def test_highlight_candidate_requires_target():
    rec = RecordingExecutor()
    with pytest.raises(ValueError):
        _run(bc.highlight_candidate(rec))


def test_highlight_candidate_encodes_target_url_safely():
    rec = RecordingExecutor(
        [{"success": True, "data": {"success": True, "result": {"highlighted": True}}, "error": None}]
    )
    target = 'https://example.com/p?id=1&q=hello world'
    result = _run(bc.highlight_candidate(rec, target_url=target))
    assert result["highlighted"] is True
    _, params = rec.calls[0]
    # JSON-encoded URL is safely embedded.
    assert json.dumps(target) in params["code"]


# ---------------------------------------------------------------------------
# extract_cart_summary / navigate
# ---------------------------------------------------------------------------


def test_extract_cart_summary_passes_through_result():
    rec = RecordingExecutor(
        [
            {
                "success": True,
                "data": {
                    "success": True,
                    "result": {
                        "line_count": 2,
                        "lines": [{"index": 0, "title": "Item"}],
                        "subtotal_text": "$19.98",
                        "buttons": [{"label": "Proceed to checkout"}],
                        "url": "https://www.amazon.com/cart",
                    },
                },
            }
        ]
    )
    result = _run(bc.extract_cart_summary(rec))
    assert result["line_count"] == 2
    assert result["subtotal_text"] == "$19.98"


def test_navigate_passes_url_to_executor():
    rec = RecordingExecutor([{"success": True, "data": {"navigated": True}, "error": None}])
    _run(bc.navigate(rec, "https://www.amazon.com"))
    assert rec.calls == [("navigate", {"url": "https://www.amazon.com"})]


# ---------------------------------------------------------------------------
# wait_for_page_ready
# ---------------------------------------------------------------------------


def test_wait_for_page_ready_issues_url_then_selector_then_page_info():
    """The helper must call wait_for_url first, then probe selectors, then page_info."""
    profile = get_profile("ebay")
    rec = RecordingExecutor(
        [
            # wait_for_url
            {"success": True, "data": {"matched": True, "url": "https://www.ebay.com/sch/i.html?_nkw=x", "waited": 250}},
            # wait_for_selector #1 — first profile card selector matches
            {"success": True, "data": {"found": True, "waited": 100}},
            # get_page_info
            {"success": True, "data": {"url": "https://www.ebay.com/sch/i.html?_nkw=x", "title": "x | eBay"}},
        ]
    )
    result = _run(bc.wait_for_page_ready(rec, profile, "https://www.ebay.com/sch/i.html?_nkw=x"))
    assert result["ready"] is True
    assert result["matched_selector"] == profile.product_card_selectors[0]
    assert result["url"] == "https://www.ebay.com/sch/i.html?_nkw=x"
    assert result["bot_check_hint"] is False

    cmd_types = [c[0] for c in rec.calls]
    assert cmd_types[0] == "wait_for_url"
    assert cmd_types[1] == "wait_for_selector"
    assert "get_page_info" in cmd_types

    # The URL substring passed must be the host without www.
    _, url_params = rec.calls[0]
    assert url_params["url_substring"] == "ebay.com"


def test_wait_for_page_ready_flags_bot_check_pages():
    profile = get_profile("amazon")
    rec = RecordingExecutor(
        [
            # wait_for_url — timed out, did not match host
            {"success": True, "data": {"matched": False, "url": "https://www.amazon.com/errors/validateCaptcha", "waited": 6000, "timeout": True}},
            # wait_for_selector — each card selector misses
            {"success": True, "data": {"found": False, "waited": 1000, "timeout": True}},
            {"success": True, "data": {"found": False, "waited": 1000, "timeout": True}},
            {"success": True, "data": {"found": False, "waited": 1000, "timeout": True}},
            # get_page_info on a captcha page
            {"success": True, "data": {"url": "https://www.amazon.com/errors/validateCaptcha?args=...", "title": "Robot Check"}},
        ]
    )
    result = _run(
        bc.wait_for_page_ready(rec, profile, "https://www.amazon.com/s?k=x")
    )
    assert result["ready"] is False
    assert result["matched_selector"] == ""
    assert result["bot_check_hint"] is True


def test_is_bot_check_url_patterns():
    assert bc.is_bot_check("https://www.amazon.com/errors/validateCaptcha?abc")
    assert bc.is_bot_check("https://www.ebay.com/some/path", title="Are you human?")
    assert bc.is_bot_check("", title="Just a moment...")
    assert not bc.is_bot_check("https://www.ebay.com/sch/i.html?_nkw=x", title="x | eBay")


# ---------------------------------------------------------------------------
# extract_product_cards_robust
# ---------------------------------------------------------------------------


def test_extract_product_cards_robust_returns_profile_results_first():
    profile = get_profile("ebay")
    rec = RecordingExecutor(
        [
            # extract_product_cards succeeds
            {
                "success": True,
                "data": {
                    "success": True,
                    "result": {
                        "candidates": [{"title": "USB-C cable", "url": "https://ebay.com/itm/1", "price_text": "$9"}],
                        "total_cards": 1,
                    },
                },
            },
        ]
    )
    result = _run(bc.extract_product_cards_robust(rec, profile, limit=10))
    assert result["source"] == "profile"
    assert result["candidates"][0]["title"] == "USB-C cable"
    # Only one eval_js call when profile selectors succeed.
    assert len([c for c in rec.calls if c[0] == "eval_js"]) == 1


def test_extract_product_cards_robust_falls_back_to_generic():
    profile = get_profile("ebay")
    rec = RecordingExecutor(
        [
            # Profile extractor returns no candidates.
            {
                "success": True,
                "data": {
                    "success": True,
                    "result": {"candidates": [], "total_cards": 0},
                },
            },
            # Generic fallback finds something.
            {
                "success": True,
                "data": {
                    "success": True,
                    "result": {
                        "candidates": [
                            {"title": "Some Generic Cable Listing", "url": "https://ebay.com/itm/2", "price_text": "$12"},
                        ],
                        "total_cards": 1,
                        "source": "generic",
                    },
                },
            },
        ]
    )
    result = _run(bc.extract_product_cards_robust(rec, profile, limit=10))
    assert result["source"] == "generic"
    assert result["candidates"][0]["title"] == "Some Generic Cable Listing"
    assert result.get("primary_total_cards") == 0
    # Two eval_js calls: one per attempt.
    assert len([c for c in rec.calls if c[0] == "eval_js"]) == 2


# ---------------------------------------------------------------------------
# detect_signin_wall
# ---------------------------------------------------------------------------


def test_detect_signin_wall_passes_addtocart_hints():
    profile = get_profile("amazon")
    rec = RecordingExecutor(
        [
            {
                "success": True,
                "data": {
                    "success": True,
                    "result": {
                        "signin_present": True,
                        "signin_controls": 3,
                        "addtocart_controls": 0,
                        "has_password_field": True,
                        "title_hint": True,
                        "hits": ["sign in"],
                        "url": "https://www.amazon.com/ap/signin",
                    },
                },
            }
        ]
    )
    result = _run(bc.detect_signin_wall(rec, profile))
    assert result["signin_present"] is True
    _, params = rec.calls[0]
    # The add-to-cart hint list is injected into the payload so the JS can
    # disambiguate "sign-in wall" from "product page with sign-in link".
    assert "add to cart" in params["code"].lower()
