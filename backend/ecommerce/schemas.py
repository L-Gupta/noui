"""Pydantic schemas for the e-commerce agent REST API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------


class StartRunRequest(BaseModel):
    """Begin a shopping task on one of the supported sites.

    The agent runs inside whichever Chrome tab the NoUI extension controls.
    For *search* on the top-20 sites no login is required. For *add to
    cart* and *proceed to checkout*, most sites (Amazon, eBay, Walmart,
    Etsy, Best Buy, Target, etc.) require an active signed-in session in
    that Chrome tab. If the agent encounters a sign-in wall on add-to-cart
    it surfaces a ``retry_add_to_cart`` checkpoint so the user can sign in
    manually and then resume by approving the retry.
    """

    site_slug: str = Field(..., description="Identifier of a site in the catalog")
    query: str = Field(..., min_length=1)
    region: str = ""
    currency: str = ""
    max_price: float | None = None
    min_rating: float | None = None
    shipping_preference: str = ""
    notes: str = ""
    auto_pilot: bool = Field(
        default=True,
        description=(
            "When true (default), the agent automatically searches, picks the "
            "top-ranked candidate, and adds it to the cart, pausing only for "
            "human confirmation right before checkout. When false, every "
            "side effect requires an explicit confirm call."
        ),
    )
    allowed_side_effects: list[str] = Field(
        default_factory=lambda: ["search", "browse", "add_to_cart"]
    )
    forbidden_side_effects: list[str] = Field(
        default_factory=lambda: [
            "place_order",
            "pay",
            "submit_payment",
            "confirm_order",
        ]
    )


class SelectCandidateRequest(BaseModel):
    """Pick a previously-surfaced candidate to add to cart."""

    candidate_id: str


class ConfirmRequest(BaseModel):
    """User response to a pending confirmation checkpoint.

    `action` is the proposed side-effect ID returned in the checkpoint
    payload. `approved` indicates whether the agent should proceed. The
    agent will never escalate beyond the configured allowed side effects
    even when ``approved`` is True for a disallowed action.
    """

    action: str
    approved: bool
    user_note: str = ""


class CancelRequest(BaseModel):
    reason: str = ""


# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------


class CandidateOut(BaseModel):
    id: str
    rank: int
    title: str
    url: str
    image_url: str
    price_text: str
    price_amount: float | None
    currency: str
    rating_text: str
    rating_value: float | None
    review_count: int | None
    seller: str
    shipping_text: str
    availability: str
    score: float | None

    model_config = {"from_attributes": True}


class CheckpointOut(BaseModel):
    """A point at which the agent paused for human confirmation."""

    action: str
    label: str
    detail: str
    candidate_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentEventOut(BaseModel):
    id: str
    event_type: str
    severity: str
    summary: str
    payload: dict[str, Any]
    timestamp: datetime

    model_config = {"from_attributes": True}


class RunOut(BaseModel):
    id: str
    status: str
    site_slug: str
    query: str
    region: str
    currency: str
    max_price: float | None
    min_rating: float | None
    shipping_preference: str
    notes: str
    allowed_side_effects: list[str]
    forbidden_side_effects: list[str]
    selected_candidate_id: str | None
    pending_checkpoint: CheckpointOut | None
    failure_reason: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class RunDetailOut(RunOut):
    candidates: list[CandidateOut] = Field(default_factory=list)
    events: list[AgentEventOut] = Field(default_factory=list)


class SiteProfileOut(BaseModel):
    slug: str
    name: str
    domains: list[str]
    regional_domains: list[str]
    home_url: str
    currency: str
    locale: str
    forbidden_action_labels: list[str]
    notes: str
