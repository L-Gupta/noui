"""Confirmation gates and forbidden-action detection for the e-commerce agent.

The agent has three layers of protection against making a real purchase
without explicit human approval:

1. ``AgentPolicy`` — per-run allow/forbid list backed by user input.
2. ``is_forbidden_label`` (site_catalog) — string-level detection of any
   button label that looks like a final order/payment action.
3. ``classify_action`` — categorizes a proposed browser action into one of
   the named side-effect categories so the policy can be consulted before
   any click is dispatched.

The goal is defense-in-depth: even if a planner is fooled into proposing
a forbidden click, the policy gate will refuse before the command queue
ever sends it to the extension.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from backend.ecommerce.site_catalog import SiteProfile, is_forbidden_label, normalize_label

# Side-effect categories used in the policy allow/forbid lists.
SIDE_EFFECT_SEARCH = "search"
SIDE_EFFECT_BROWSE = "browse"
SIDE_EFFECT_ADD_TO_CART = "add_to_cart"
SIDE_EFFECT_PROCEED_TO_CHECKOUT = "proceed_to_checkout"
SIDE_EFFECT_FILL_SHIPPING = "fill_shipping"
SIDE_EFFECT_PLACE_ORDER = "place_order"
SIDE_EFFECT_PAY = "pay"
SIDE_EFFECT_SUBMIT_PAYMENT = "submit_payment"
SIDE_EFFECT_CONFIRM_ORDER = "confirm_order"

ALL_SIDE_EFFECTS: tuple[str, ...] = (
    SIDE_EFFECT_SEARCH,
    SIDE_EFFECT_BROWSE,
    SIDE_EFFECT_ADD_TO_CART,
    SIDE_EFFECT_PROCEED_TO_CHECKOUT,
    SIDE_EFFECT_FILL_SHIPPING,
    SIDE_EFFECT_PLACE_ORDER,
    SIDE_EFFECT_PAY,
    SIDE_EFFECT_SUBMIT_PAYMENT,
    SIDE_EFFECT_CONFIRM_ORDER,
)

# Side effects that always require explicit human confirmation regardless of
# how a run was configured.
HARD_FORBIDDEN: frozenset[str] = frozenset(
    {
        SIDE_EFFECT_PLACE_ORDER,
        SIDE_EFFECT_PAY,
        SIDE_EFFECT_SUBMIT_PAYMENT,
        SIDE_EFFECT_CONFIRM_ORDER,
    }
)


@dataclass(frozen=True)
class PolicyDecision:
    """Result of consulting an `AgentPolicy` for a proposed action."""

    allowed: bool
    requires_confirmation: bool
    reason: str = ""


@dataclass(frozen=True)
class AgentPolicy:
    """Per-run allow/forbid policy.

    `allowed_side_effects` enumerates the side-effect categories the agent
    may perform on its own. Any side effect outside that list (and not in
    `HARD_FORBIDDEN`) requires user confirmation before execution. Side
    effects in `HARD_FORBIDDEN` are never auto-executed and can only be
    surfaced as confirmation prompts the user must explicitly approve in a
    later, separately-authorized flow (not implemented in this agent).
    """

    allowed_side_effects: frozenset[str]
    forbidden_side_effects: frozenset[str]

    # Default allow-list when the caller passes None. Mirrors the StartRunRequest
    # schema default so direct callers of `create_run` get the same safe defaults
    # as REST clients. Explicitly passing an empty list still narrows the policy
    # to just search/browse.
    DEFAULT_ALLOW: tuple[str, ...] = (
        SIDE_EFFECT_SEARCH,
        SIDE_EFFECT_BROWSE,
        SIDE_EFFECT_ADD_TO_CART,
    )

    @classmethod
    def from_lists(
        cls,
        allowed: Iterable[str] | None,
        forbidden: Iterable[str] | None,
    ) -> AgentPolicy:
        if allowed is None:
            allow: set[str] = set(cls.DEFAULT_ALLOW)
        else:
            allow = {SIDE_EFFECT_SEARCH, SIDE_EFFECT_BROWSE}
            allow.update(s.strip().lower() for s in allowed if s and s.strip())
        forbid = set(HARD_FORBIDDEN)
        if forbidden:
            forbid.update(s.strip().lower() for s in forbidden if s and s.strip())
        # Anything explicitly forbidden wins over anything allowed.
        allow.difference_update(forbid)
        return cls(frozenset(allow), frozenset(forbid))

    def evaluate(self, side_effect: str) -> PolicyDecision:
        normalized = side_effect.strip().lower()
        if not normalized:
            return PolicyDecision(allowed=False, requires_confirmation=False, reason="empty side effect")
        if normalized in self.forbidden_side_effects:
            return PolicyDecision(
                allowed=False,
                requires_confirmation=True,
                reason=f"{normalized} is forbidden for this run",
            )
        if normalized in self.allowed_side_effects:
            return PolicyDecision(allowed=True, requires_confirmation=False)
        # Unknown side effect: not explicitly forbidden, not explicitly
        # allowed. We force a confirmation prompt rather than refusing
        # outright, so the user can authorize unusual flows on a case-by-case
        # basis without expanding the global policy.
        return PolicyDecision(
            allowed=False,
            requires_confirmation=True,
            reason=f"{normalized} requires explicit user confirmation",
        )


# ---------------------------------------------------------------------------
# Action classification
# ---------------------------------------------------------------------------


# Substrings -> side effect. Order matters: more specific phrases first so
# "submit payment" classifies as submit_payment, not as a generic submit.
_LABEL_RULES: tuple[tuple[str, str], ...] = (
    ("submit payment", SIDE_EFFECT_SUBMIT_PAYMENT),
    ("authorize payment", SIDE_EFFECT_SUBMIT_PAYMENT),
    ("authorise payment", SIDE_EFFECT_SUBMIT_PAYMENT),
    ("pay now", SIDE_EFFECT_PAY),
    ("pay with", SIDE_EFFECT_PAY),
    ("place your order", SIDE_EFFECT_PLACE_ORDER),
    ("place order", SIDE_EFFECT_PLACE_ORDER),
    ("submit order", SIDE_EFFECT_PLACE_ORDER),
    ("complete order", SIDE_EFFECT_PLACE_ORDER),
    ("confirm order", SIDE_EFFECT_CONFIRM_ORDER),
    ("confirm purchase", SIDE_EFFECT_CONFIRM_ORDER),
    ("confirm and pay", SIDE_EFFECT_CONFIRM_ORDER),
    ("complete purchase", SIDE_EFFECT_CONFIRM_ORDER),
    ("complete payment", SIDE_EFFECT_SUBMIT_PAYMENT),
    ("buy now", SIDE_EFFECT_PLACE_ORDER),
    ("buy it now", SIDE_EFFECT_PLACE_ORDER),
    ("proceed to checkout", SIDE_EFFECT_PROCEED_TO_CHECKOUT),
    ("go to checkout", SIDE_EFFECT_PROCEED_TO_CHECKOUT),
    ("continue to checkout", SIDE_EFFECT_PROCEED_TO_CHECKOUT),
    ("checkout", SIDE_EFFECT_PROCEED_TO_CHECKOUT),
    ("add to cart", SIDE_EFFECT_ADD_TO_CART),
    ("add to bag", SIDE_EFFECT_ADD_TO_CART),
    ("add to basket", SIDE_EFFECT_ADD_TO_CART),
    ("save shipping", SIDE_EFFECT_FILL_SHIPPING),
    ("use this address", SIDE_EFFECT_FILL_SHIPPING),
)


def classify_action(label: str, profile: SiteProfile | None = None) -> str:
    """Return the canonical side-effect category for a button label.

    Falls back to ``browse`` for navigation-style clicks and labels we
    cannot classify. Always honors `is_forbidden_label`: if the label
    matches a hard-forbidden phrase but did not match a more specific rule
    above, classify as ``place_order`` to keep the safety bar high.
    """
    needle = normalize_label(label)
    if not needle:
        return SIDE_EFFECT_BROWSE
    for phrase, category in _LABEL_RULES:
        if phrase in needle:
            return category
    if is_forbidden_label(label, profile):
        return SIDE_EFFECT_PLACE_ORDER
    return SIDE_EFFECT_BROWSE


def is_hard_forbidden(side_effect: str) -> bool:
    """True when the side effect can never be auto-executed by the agent."""
    return side_effect.strip().lower() in HARD_FORBIDDEN


__all__ = [
    "ALL_SIDE_EFFECTS",
    "AgentPolicy",
    "HARD_FORBIDDEN",
    "PolicyDecision",
    "SIDE_EFFECT_ADD_TO_CART",
    "SIDE_EFFECT_BROWSE",
    "SIDE_EFFECT_CONFIRM_ORDER",
    "SIDE_EFFECT_FILL_SHIPPING",
    "SIDE_EFFECT_PAY",
    "SIDE_EFFECT_PLACE_ORDER",
    "SIDE_EFFECT_PROCEED_TO_CHECKOUT",
    "SIDE_EFFECT_SEARCH",
    "SIDE_EFFECT_SUBMIT_PAYMENT",
    "classify_action",
    "is_hard_forbidden",
]
