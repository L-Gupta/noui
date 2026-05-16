"""ORM models for the e-commerce agent.

State for a single shopping task is split across three tables so the audit
trail can be inspected independently of the run itself:

- `EcommerceAgentRun`            : top-level task and state machine
- `EcommerceProductCandidate`    : product candidates surfaced during a run
- `EcommerceAgentEvent`          : append-only audit log (browser actions,
                                   prompts, confirmations, errors)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_uuid() -> str:
    return str(uuid.uuid4())


class EcommerceAgentRun(Base):
    """One shopping task, executed against one e-commerce site.

    The run owns the high-level state machine. Detailed step records live in
    `EcommerceAgentEvent`; surfaced products live in
    `EcommerceProductCandidate`.

    Status lifecycle:
        created -> searching -> awaiting_selection -> selected ->
        adding_to_cart -> awaiting_cart_confirmation -> in_cart ->
        awaiting_checkout_confirmation -> at_checkout_boundary -> completed
        Any state can transition to: needs_user | cancelled | failed

    Final-state semantics:
        completed                 — agent finished within the allowed scope
                                    (cart prepared and/or checkout boundary
                                    reached and acknowledged by the user).
        at_checkout_boundary      — agent intentionally stopped before any
                                    payment/order-placement action.
        needs_user                — agent paused for human input (captcha,
                                    MFA, missing variant, etc.).
        cancelled                 — user cancelled the run.
        failed                    — agent could not proceed safely.
    """

    __tablename__ = "ecommerce_agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)

    status: Mapped[str] = mapped_column(String(40), default="created")

    site_slug: Mapped[str] = mapped_column(String(50))
    query: Mapped[str] = mapped_column(Text)

    # Request constraints (optional)
    region: Mapped[str] = mapped_column(String(20), default="")
    currency: Mapped[str] = mapped_column(String(10), default="")
    max_price: Mapped[str] = mapped_column(String(40), default="")
    min_rating: Mapped[str] = mapped_column(String(40), default="")
    shipping_preference: Mapped[str] = mapped_column(String(40), default="")
    notes: Mapped[str] = mapped_column(Text, default="")

    # Safety configuration (JSON-encoded list/dict). Defaults are conservative.
    allowed_side_effects_json: Mapped[str] = mapped_column(Text, default='["search","browse","add_to_cart"]')
    forbidden_side_effects_json: Mapped[str] = mapped_column(
        Text, default='["place_order","pay","submit_payment","confirm_order"]'
    )

    # Selected product (set when the user picks one of the candidates)
    selected_candidate_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Last confirmation checkpoint metadata (JSON-encoded). Filled when the
    # agent is waiting on the user. Cleared when the user responds.
    pending_checkpoint_json: Mapped[str] = mapped_column(Text, default="")

    # Failure / cancellation reason
    failure_reason: Mapped[str] = mapped_column(Text, default="")

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)


class EcommerceProductCandidate(Base):
    """A single product surfaced for an agent run.

    Candidates are produced from the live page via the extension's
    extract_product_cards command, then ranked and persisted so the user
    can later choose one via the select endpoint.
    """

    __tablename__ = "ecommerce_product_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("ecommerce_agent_runs.id"))

    rank: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str] = mapped_column(Text, default="")
    price_text: Mapped[str] = mapped_column(String(80), default="")
    price_amount: Mapped[str] = mapped_column(String(40), default="")
    currency: Mapped[str] = mapped_column(String(10), default="")
    rating_text: Mapped[str] = mapped_column(String(40), default="")
    rating_value: Mapped[str] = mapped_column(String(20), default="")
    review_count: Mapped[str] = mapped_column(String(20), default="")
    seller: Mapped[str] = mapped_column(String(200), default="")
    shipping_text: Mapped[str] = mapped_column(String(200), default="")
    availability: Mapped[str] = mapped_column(String(60), default="")
    score: Mapped[str] = mapped_column(String(40), default="")

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class EcommerceAgentEvent(Base):
    """Append-only audit log for a single run.

    Every browser command, planner decision, candidate ranking, user prompt,
    user confirmation, and error becomes one row. Severity is one of
    ``info``, ``warn``, ``error``, ``user``.
    """

    __tablename__ = "ecommerce_agent_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("ecommerce_agent_runs.id"))

    event_type: Mapped[str] = mapped_column(String(60))
    severity: Mapped[str] = mapped_column(String(20), default="info")
    summary: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(default=_utcnow)
