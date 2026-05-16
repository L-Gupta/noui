"""Tests for the e-commerce agent's safety policy and action classifier."""

from __future__ import annotations

import sys
from pathlib import Path

_NOUI_ROOT = Path(__file__).resolve().parent.parent
if str(_NOUI_ROOT) not in sys.path:
    sys.path.insert(0, str(_NOUI_ROOT))

import pytest

from backend.ecommerce.safety import (
    HARD_FORBIDDEN,
    SIDE_EFFECT_ADD_TO_CART,
    SIDE_EFFECT_BROWSE,
    SIDE_EFFECT_CONFIRM_ORDER,
    SIDE_EFFECT_PAY,
    SIDE_EFFECT_PLACE_ORDER,
    SIDE_EFFECT_PROCEED_TO_CHECKOUT,
    SIDE_EFFECT_SUBMIT_PAYMENT,
    AgentPolicy,
    classify_action,
    is_hard_forbidden,
)
from backend.ecommerce.site_catalog import get_profile

# ---------------------------------------------------------------------------
# classify_action
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Add to cart", SIDE_EFFECT_ADD_TO_CART),
        ("Add to Bag", SIDE_EFFECT_ADD_TO_CART),
        ("Add to basket", SIDE_EFFECT_ADD_TO_CART),
        ("Proceed to checkout", SIDE_EFFECT_PROCEED_TO_CHECKOUT),
        ("Go to Checkout", SIDE_EFFECT_PROCEED_TO_CHECKOUT),
        ("Checkout", SIDE_EFFECT_PROCEED_TO_CHECKOUT),
        ("Place Your Order", SIDE_EFFECT_PLACE_ORDER),
        ("Place order", SIDE_EFFECT_PLACE_ORDER),
        ("Submit order", SIDE_EFFECT_PLACE_ORDER),
        ("Buy now", SIDE_EFFECT_PLACE_ORDER),
        ("Pay now", SIDE_EFFECT_PAY),
        ("Pay with PayPal", SIDE_EFFECT_PAY),
        ("Submit Payment", SIDE_EFFECT_SUBMIT_PAYMENT),
        ("Confirm and pay", SIDE_EFFECT_CONFIRM_ORDER),
        ("Confirm purchase", SIDE_EFFECT_CONFIRM_ORDER),
        ("Complete purchase", SIDE_EFFECT_CONFIRM_ORDER),
        ("View product", SIDE_EFFECT_BROWSE),
        ("See more", SIDE_EFFECT_BROWSE),
    ],
)
def test_classify_action(label, expected):
    assert classify_action(label) == expected


def test_classify_action_uses_profile_for_localized_labels():
    rakuten = get_profile("rakuten")
    # Japanese "place order" classifies as place_order via the forbidden-label
    # fallback, since the rules table is English.
    assert classify_action("注文を確定する", rakuten) == SIDE_EFFECT_PLACE_ORDER


def test_classify_action_empty_string():
    assert classify_action("") == SIDE_EFFECT_BROWSE


# ---------------------------------------------------------------------------
# AgentPolicy.from_lists
# ---------------------------------------------------------------------------


def test_policy_default_allows_search_browse():
    p = AgentPolicy.from_lists([], [])
    assert "search" in p.allowed_side_effects
    assert "browse" in p.allowed_side_effects


def test_policy_hard_forbidden_always_blocked():
    p = AgentPolicy.from_lists(
        ["search", "browse", "place_order"],  # user tries to allow place_order
        [],
    )
    # Hard-forbidden side effects win over an explicit allow.
    assert "place_order" not in p.allowed_side_effects
    for forbidden in HARD_FORBIDDEN:
        assert forbidden in p.forbidden_side_effects


def test_policy_explicit_forbid_beats_allow():
    p = AgentPolicy.from_lists(
        ["search", "browse", "add_to_cart"],
        ["add_to_cart"],
    )
    assert "add_to_cart" not in p.allowed_side_effects
    assert "add_to_cart" in p.forbidden_side_effects


def test_policy_evaluate_allowed():
    p = AgentPolicy.from_lists(["add_to_cart"], [])
    d = p.evaluate("add_to_cart")
    assert d.allowed and not d.requires_confirmation


def test_policy_evaluate_unknown_requires_confirmation():
    p = AgentPolicy.from_lists(["add_to_cart"], [])
    d = p.evaluate("fill_shipping")
    assert d.allowed is False
    assert d.requires_confirmation is True


def test_policy_evaluate_hard_forbidden_never_allowed():
    p = AgentPolicy.from_lists(["place_order"], [])  # ignored
    d = p.evaluate("place_order")
    assert d.allowed is False
    assert d.requires_confirmation is True


def test_policy_evaluate_empty_action():
    p = AgentPolicy.from_lists(["add_to_cart"], [])
    d = p.evaluate("")
    assert d.allowed is False


# ---------------------------------------------------------------------------
# Hard-forbidden helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        "place_order",
        "Place_Order",
        "  PAY  ",
        "submit_payment",
        "confirm_order",
    ],
)
def test_is_hard_forbidden(action):
    assert is_hard_forbidden(action) is True


@pytest.mark.parametrize("action", ["search", "browse", "add_to_cart", "proceed_to_checkout"])
def test_is_hard_forbidden_negative(action):
    assert is_hard_forbidden(action) is False
