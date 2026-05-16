"""E-commerce agent core: run state machine, planner, and audit trail.

The agent is *not* a fully autonomous LLM: it composes a small number of
deterministic browser actions per state transition, persists every action
to the audit log, and pauses for explicit user confirmation before any
side effect that is not in the run's allow-list.

This module never clicks final order/payment buttons under any
circumstance — the policy gate + detection step in `safety.py` enforce
that contract.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.ecommerce import browser_commands as bc
from backend.ecommerce.models import (
    EcommerceAgentEvent,
    EcommerceAgentRun,
    EcommerceProductCandidate,
)
from backend.ecommerce.ranking import normalize_candidate, rank_candidates
from backend.ecommerce.safety import (
    HARD_FORBIDDEN,
    SIDE_EFFECT_ADD_TO_CART,
    SIDE_EFFECT_PROCEED_TO_CHECKOUT,
    AgentPolicy,
    classify_action,
)
from backend.ecommerce.site_catalog import (
    all_slugs,
    get_profile,
    is_forbidden_label,
)

logger = logging.getLogger(__name__)


# Valid run statuses (kept loose; documented in models.py).
class RunStatus:
    CREATED = "created"
    SEARCHING = "searching"
    AWAITING_SELECTION = "awaiting_selection"
    SELECTED = "selected"
    ADDING_TO_CART = "adding_to_cart"
    AWAITING_CART_CONFIRMATION = "awaiting_cart_confirmation"
    IN_CART = "in_cart"
    AWAITING_CHECKOUT_CONFIRMATION = "awaiting_checkout_confirmation"
    AT_CHECKOUT_BOUNDARY = "at_checkout_boundary"
    COMPLETED = "completed"
    NEEDS_USER = "needs_user"
    CANCELLED = "cancelled"
    FAILED = "failed"


# Default candidate cap surfaced to the user per search.
MAX_CANDIDATES = 12

# Retry actions reuse an existing side-effect's policy entry. The retry
# action is just a labelled checkpoint that re-runs the same handler after
# a transient block (sign-in wall, captcha) has been cleared.
_ACTION_ALIAS: dict[str, str] = {
    "retry_add_to_cart": SIDE_EFFECT_ADD_TO_CART,
}


@dataclass(frozen=True)
class Checkpoint:
    """Confirmation prompt the user must answer before the agent continues."""

    action: str
    label: str
    detail: str
    candidate_id: str | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "label": self.label,
            "detail": self.detail,
            "candidate_id": self.candidate_id,
            "metadata": self.metadata or {},
        }


# ---------------------------------------------------------------------------
# Audit log helpers
# ---------------------------------------------------------------------------


async def _log_event(
    db: AsyncSession,
    run: EcommerceAgentRun,
    *,
    event_type: str,
    summary: str = "",
    payload: dict[str, Any] | None = None,
    severity: str = "info",
) -> None:
    event = EcommerceAgentEvent(
        run_id=run.id,
        event_type=event_type,
        severity=severity,
        summary=summary,
        payload_json=json.dumps(payload or {}, default=str),
    )
    db.add(event)
    # We flush so concurrent reads see the new event without waiting for a
    # higher-level commit, but the commit is the caller's responsibility.
    await db.flush()


def _decode_list(json_text: str) -> list[str]:
    try:
        value = json.loads(json_text or "[]")
        if isinstance(value, list):
            return [str(item) for item in value]
    except json.JSONDecodeError:
        logger.warning("Failed to decode allow/forbid JSON: %r", json_text)
    return []


def _policy_for(run: EcommerceAgentRun) -> AgentPolicy:
    return AgentPolicy.from_lists(
        _decode_list(run.allowed_side_effects_json),
        _decode_list(run.forbidden_side_effects_json),
    )


def _set_checkpoint(run: EcommerceAgentRun, checkpoint: Checkpoint | None) -> None:
    run.pending_checkpoint_json = json.dumps(checkpoint.to_dict()) if checkpoint else ""


def get_checkpoint(run: EcommerceAgentRun) -> Checkpoint | None:
    raw = run.pending_checkpoint_json
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return Checkpoint(
        action=data.get("action", ""),
        label=data.get("label", ""),
        detail=data.get("detail", ""),
        candidate_id=data.get("candidate_id"),
        metadata=data.get("metadata") or {},
    )


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------


async def create_run(
    db: AsyncSession,
    *,
    site_slug: str,
    query: str,
    region: str = "",
    currency: str = "",
    max_price: float | None = None,
    min_rating: float | None = None,
    shipping_preference: str = "",
    notes: str = "",
    allowed_side_effects: list[str] | None = None,
    forbidden_side_effects: list[str] | None = None,
) -> EcommerceAgentRun:
    """Create a new agent run record after validating the site slug."""
    slug = site_slug.strip().lower()
    if slug not in all_slugs():
        raise ValueError(
            f"Unknown e-commerce site slug: {site_slug!r}. "
            f"Valid slugs: {', '.join(all_slugs())}"
        )
    if not query.strip():
        raise ValueError("query must be non-empty")

    policy = AgentPolicy.from_lists(allowed_side_effects, forbidden_side_effects)

    run = EcommerceAgentRun(
        status=RunStatus.CREATED,
        site_slug=slug,
        query=query.strip(),
        region=region.strip(),
        currency=currency.strip().upper(),
        max_price=str(max_price) if max_price is not None else "",
        min_rating=str(min_rating) if min_rating is not None else "",
        shipping_preference=shipping_preference.strip(),
        notes=notes.strip(),
        allowed_side_effects_json=json.dumps(sorted(policy.allowed_side_effects)),
        forbidden_side_effects_json=json.dumps(sorted(policy.forbidden_side_effects)),
    )
    db.add(run)
    await db.flush()
    await _log_event(
        db,
        run,
        event_type="run_created",
        summary=f"Run created for {slug} query={query!r}",
        payload={
            "allowed": sorted(policy.allowed_side_effects),
            "forbidden": sorted(policy.forbidden_side_effects),
        },
    )
    await db.commit()
    await db.refresh(run)
    return run


async def search(
    db: AsyncSession,
    run: EcommerceAgentRun,
    executor: bc.BrowserExec,
) -> EcommerceAgentRun:
    """Drive the browser to the search results page and surface candidates.

    On success the run transitions to ``awaiting_selection`` with a list of
    `EcommerceProductCandidate` rows attached.
    """
    profile = get_profile(run.site_slug)
    run.status = RunStatus.SEARCHING
    run.updated_at = datetime.now(UTC)
    search_url = profile.build_search_url(run.query)
    await _log_event(
        db,
        run,
        event_type="search_start",
        summary=f"Navigating to {search_url}",
        payload={"site": run.site_slug, "query": run.query, "url": search_url},
    )
    await db.commit()

    try:
        await bc.navigate(executor, search_url)
    except Exception as exc:  # pragma: no cover — bridge errors are reported back
        await _fail_run(db, run, f"navigation failed: {exc}", event_type="navigate_failed")
        raise

    # Critical: chrome.tabs.update() resolves the moment navigation is
    # initiated, not when the new page is loaded. Without an explicit wait
    # the extractor below would race against the previous page (or an empty
    # document) and return zero cards. Wait for both URL and at least one
    # card selector to materialize before scraping.
    ready_info: dict[str, Any] = {}
    try:
        ready_info = await bc.wait_for_page_ready(executor, profile, search_url)
    except Exception as exc:  # pragma: no cover — diagnostic only
        logger.debug("wait_for_page_ready failed: %s", exc)
    await _log_event(
        db,
        run,
        event_type="page_ready",
        summary=(
            f"Page ready={ready_info.get('ready', False)} "
            f"selector={ready_info.get('matched_selector', '') or 'none'} "
            f"url={ready_info.get('url', '') or search_url}"
        ),
        payload=ready_info,
    )

    try:
        raw = await bc.extract_product_cards_robust(
            executor, profile, limit=MAX_CANDIDATES
        )
    except Exception as exc:  # pragma: no cover — defensive
        await _fail_run(db, run, f"product extraction failed: {exc}", event_type="extract_failed")
        raise

    raw_cards = raw.get("candidates") or []
    normalized = [
        normalize_candidate(card, default_currency=profile.currency) for card in raw_cards
    ]
    normalized = [c for c in normalized if c["title"]]
    max_price = _safe_float(run.max_price)
    min_rating = _safe_float(run.min_rating)
    ranked = rank_candidates(
        normalized,
        max_price=max_price,
        min_rating=min_rating,
    )

    for c in ranked:
        candidate = EcommerceProductCandidate(
            run_id=run.id,
            rank=c["rank"],
            title=c["title"][:1000],
            url=c["url"][:1000],
            image_url=c["image_url"][:1000],
            price_text=c["price_text"][:80],
            price_amount=_fmt_number(c["price_amount"]),
            currency=(c["currency"] or "")[:10],
            rating_text=c["rating_text"][:40],
            rating_value=_fmt_number(c["rating_value"]),
            review_count=_fmt_number(c["review_count"]),
            seller=c["seller"][:200],
            shipping_text=c["shipping_text"][:200],
            availability=c["availability"][:60],
            score=_fmt_number(c["score"]),
        )
        db.add(candidate)

    if not ranked:
        bot_hint = bool(ready_info.get("bot_check_hint"))
        final_url = ready_info.get("url", "") or search_url
        page_title = ready_info.get("title", "")
        await _log_event(
            db,
            run,
            event_type="search_empty",
            severity="warn",
            summary="No product candidates found",
            payload={
                "raw_total": raw.get("total_cards", 0),
                "primary_total_cards": raw.get("primary_total_cards", 0),
                "extractor_source": raw.get("source", ""),
                "matched_selector": ready_info.get("matched_selector", ""),
                "ready": ready_info.get("ready", False),
                "final_url": final_url,
                "page_title": page_title,
                "bot_check_hint": bot_hint,
            },
        )
        run.status = RunStatus.NEEDS_USER
        if bot_hint:
            detail = (
                f"{profile.name} appears to be showing a captcha or bot-check page "
                f"(url={final_url!r}, title={page_title!r}). Solve it in the browser "
                f"tab, then start a new run. The query was {run.query!r}."
            )
        elif not ready_info.get("ready", False) and not ready_info.get("matched_selector"):
            detail = (
                f"The search results page never finished loading the expected product "
                f"cards on {profile.name} (url={final_url!r}). The selectors may be "
                f"out of date, or the page may need user interaction (cookie banner, "
                f"region picker). Query: {run.query!r}."
            )
        else:
            detail = (
                f"No product candidates were detected for query {run.query!r} on "
                f"{profile.name}. The site rendered (url={final_url!r}) but neither "
                f"the site-specific extractor nor the generic price+link fallback "
                f"matched any cards. The selectors may need updating."
            )
        _set_checkpoint(
            run,
            Checkpoint(
                action="select_product",
                label="No products found",
                detail=detail,
                metadata={
                    "final_url": final_url,
                    "page_title": page_title,
                    "bot_check_hint": bot_hint,
                },
            ),
        )
    else:
        run.status = RunStatus.AWAITING_SELECTION
        _set_checkpoint(
            run,
            Checkpoint(
                action="select_product",
                label="Pick a candidate",
                detail=(
                    f"Found {len(ranked)} candidate{'s' if len(ranked) != 1 else ''} for "
                    f"{run.query!r} on {profile.name}. Choose one to add to cart."
                ),
                metadata={"candidate_count": len(ranked)},
            ),
        )
        await _log_event(
            db,
            run,
            event_type="search_ranked",
            summary=f"Ranked {len(ranked)} candidates",
            payload={"count": len(ranked)},
        )

    run.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(run)
    return run


async def auto_advance_after_search(
    db: AsyncSession,
    run: EcommerceAgentRun,
    executor: bc.BrowserExec,
) -> EcommerceAgentRun:
    """Run the full pre-checkout pipeline without waiting for user input.

    Picks the top-ranked candidate, adds it to the cart, then leaves the
    run at ``awaiting_checkout_confirmation`` so the user only has to
    approve the proceed-to-checkout step. Used by the ``auto_pilot``
    mode on ``POST /runs``.

    No-op when the run is not in ``awaiting_selection`` (e.g. a captcha
    interstitial left it at ``needs_user``).
    """
    if run.status != RunStatus.AWAITING_SELECTION:
        return run

    candidates = await list_candidates(db, run.id)
    if not candidates:
        return run

    top = candidates[0]
    await _log_event(
        db,
        run,
        event_type="auto_selected",
        summary=f"Auto-picked top candidate rank={top.rank} title={top.title!r}",
        payload={"candidate_id": top.id, "title": top.title, "url": top.url},
    )
    run, _ = await select_candidate(db, run, executor, top.id)

    # Skip the manual add-to-cart confirmation and execute it directly.
    if run.status != RunStatus.AWAITING_CART_CONFIRMATION:
        return run

    await _log_event(
        db,
        run,
        event_type="auto_add_to_cart",
        summary="Auto-confirming add-to-cart without user prompt",
    )
    _set_checkpoint(run, None)
    await _execute_add_to_cart(db, run, executor)
    run.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(run)
    return run


async def select_candidate(
    db: AsyncSession,
    run: EcommerceAgentRun,
    executor: bc.BrowserExec,
    candidate_id: str,
) -> tuple[EcommerceAgentRun, Checkpoint]:
    """Record the user's selection and ask whether to add the item to cart.

    The agent does **not** click "Add to cart" here — even though that
    action is usually in the allow-list, we still surface a confirmation
    checkpoint so the user has a clear record of every side effect.
    """
    candidate = await _get_candidate(db, run, candidate_id)

    run.selected_candidate_id = candidate.id
    run.status = RunStatus.SELECTED
    await _log_event(
        db,
        run,
        event_type="candidate_selected",
        summary=f"User selected candidate rank={candidate.rank} title={candidate.title!r}",
        payload={
            "candidate_id": candidate.id,
            "rank": candidate.rank,
            "title": candidate.title,
            "url": candidate.url,
            "price_text": candidate.price_text,
        },
        severity="user",
    )

    # Best-effort highlight so the user can visually confirm the choice.
    try:
        await bc.highlight_candidate(
            executor,
            target_url=candidate.url,
            target_text=candidate.title,
        )
    except Exception as exc:  # pragma: no cover — best effort
        logger.debug("highlight_candidate failed: %s", exc)

    checkpoint = Checkpoint(
        action="add_to_cart",
        label="Add to cart?",
        detail=(
            f"Add {candidate.title!r} ({candidate.price_text or 'no price detected'}) "
            "to the shopping cart? The agent will only add the item — it will never "
            "place an order or submit payment."
        ),
        candidate_id=candidate.id,
        metadata={
            "title": candidate.title,
            "url": candidate.url,
            "price_text": candidate.price_text,
        },
    )
    _set_checkpoint(run, checkpoint)
    run.status = RunStatus.AWAITING_CART_CONFIRMATION
    run.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(run)
    return run, checkpoint


async def confirm(
    db: AsyncSession,
    run: EcommerceAgentRun,
    executor: bc.BrowserExec,
    *,
    action: str,
    approved: bool,
    user_note: str = "",
) -> EcommerceAgentRun:
    """Apply the user's response to the run's pending confirmation checkpoint.

    Honors the policy gate even when ``approved`` is True: anything in
    ``HARD_FORBIDDEN`` is rejected with an audit log entry rather than
    silently executed.
    """
    checkpoint = get_checkpoint(run)
    if checkpoint is None or checkpoint.action != action:
        raise ValueError(
            f"No matching pending checkpoint for action={action!r}. "
            f"Current checkpoint: {checkpoint.action if checkpoint else None!r}"
        )

    if not approved:
        await _log_event(
            db,
            run,
            event_type="user_declined",
            summary=f"User declined action={action!r}",
            payload={"note": user_note},
            severity="user",
        )
        run.status = RunStatus.CANCELLED
        run.completed_at = datetime.now(UTC)
        _set_checkpoint(run, None)
        run.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(run)
        return run

    policy = _policy_for(run)
    # Retry actions map back to the underlying side effect for policy
    # evaluation — they exist only to label the checkpoint distinctly.
    policy_action = _ACTION_ALIAS.get(action, action)
    decision = policy.evaluate(policy_action)
    if not decision.allowed or policy_action in HARD_FORBIDDEN:
        await _log_event(
            db,
            run,
            event_type="policy_blocked",
            severity="error",
            summary=f"Refused user approval for {action!r}: {decision.reason or 'hard-forbidden'}",
            payload={"action": action},
        )
        run.status = RunStatus.NEEDS_USER
        run.failure_reason = (
            f"Action {action!r} is not allowed by this run's policy. "
            "Cancel and start a new run with the action in allowed_side_effects to override."
        )
        run.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(run)
        return run

    await _log_event(
        db,
        run,
        event_type="user_approved",
        summary=f"User approved action={action!r}",
        payload={"note": user_note},
        severity="user",
    )
    _set_checkpoint(run, None)

    if action == SIDE_EFFECT_ADD_TO_CART:
        await _execute_add_to_cart(db, run, executor)
    elif action == "retry_add_to_cart":
        # User signed in (or cleared a blocker) and approved a retry.
        run.failure_reason = ""
        await _execute_add_to_cart(db, run, executor)
    elif action == SIDE_EFFECT_PROCEED_TO_CHECKOUT:
        await _execute_proceed_to_checkout(db, run, executor)
    else:
        await _log_event(
            db,
            run,
            event_type="action_noop",
            severity="warn",
            summary=f"No handler for approved action={action!r}",
            payload={"action": action},
        )

    run.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(run)
    return run


async def cancel_run(
    db: AsyncSession,
    run: EcommerceAgentRun,
    reason: str = "",
) -> EcommerceAgentRun:
    run.status = RunStatus.CANCELLED
    run.failure_reason = reason
    run.completed_at = datetime.now(UTC)
    _set_checkpoint(run, None)
    await _log_event(
        db,
        run,
        event_type="run_cancelled",
        severity="user",
        summary=reason or "Run cancelled by user",
    )
    run.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(run)
    return run


# ---------------------------------------------------------------------------
# Internal actions
# ---------------------------------------------------------------------------


async def _execute_add_to_cart(
    db: AsyncSession,
    run: EcommerceAgentRun,
    executor: bc.BrowserExec,
) -> None:
    """Navigate to the product, click Add to cart, then surface cart summary."""
    if run.selected_candidate_id is None:
        raise ValueError("Cannot add to cart: no candidate selected")
    candidate = await _get_candidate(db, run, run.selected_candidate_id)
    profile = get_profile(run.site_slug)

    run.status = RunStatus.ADDING_TO_CART
    await _log_event(
        db,
        run,
        event_type="adding_to_cart",
        summary=f"Navigating to product {candidate.url}",
        payload={"candidate_id": candidate.id, "url": candidate.url},
    )

    if candidate.url:
        try:
            await bc.navigate(executor, candidate.url)
        except Exception as exc:
            await _fail_run(db, run, f"product navigation failed: {exc}")
            return

    # Always re-check boundary BEFORE clicking anything on the product page.
    # Some sites surface a "Buy now" button right next to "Add to cart".
    boundary = await bc.detect_purchase_boundary(executor, profile)
    if boundary.get("boundary_present"):
        await _log_event(
            db,
            run,
            event_type="boundary_detected",
            severity="warn",
            summary="Forbidden action button visible on product page",
            payload=boundary,
        )

    # Look for an add-to-cart label appropriate for this site (English fallback first).
    labels = list(profile.add_to_cart_labels) or ["add to cart"]
    clicked: dict[str, Any] = {"clicked": False}
    for label in labels:
        if is_forbidden_label(label, profile):
            # Refuse to click a label that classifies as forbidden, even if
            # the profile mistakenly lists it.
            await _log_event(
                db,
                run,
                event_type="policy_blocked",
                severity="error",
                summary=f"Refused to click label {label!r} — classifies as forbidden",
                payload={"label": label, "classified_as": classify_action(label, profile)},
            )
            continue
        try:
            result = await bc.click_by_text(executor, label)
        except Exception as exc:
            await _log_event(
                db,
                run,
                event_type="click_failed",
                severity="warn",
                summary=f"click_by_text({label!r}) failed: {exc}",
            )
            continue
        if result.get("clicked"):
            clicked = result
            await _log_event(
                db,
                run,
                event_type="add_to_cart_click",
                summary=f"Clicked add-to-cart label {label!r}",
                payload={"label": label, "result": result},
            )
            break

    if not clicked.get("clicked"):
        # Distinguish three failure modes: (1) sign-in wall, (2) bot
        # mitigation, (3) genuine selector miss. Surface a targeted
        # checkpoint so the user knows exactly what to do.
        signin: dict[str, Any] = {}
        try:
            signin = await bc.detect_signin_wall(executor, profile)
        except Exception as exc:  # pragma: no cover — diagnostic only
            logger.debug("detect_signin_wall failed: %s", exc)

        if signin.get("signin_present"):
            await _log_event(
                db,
                run,
                event_type="signin_wall_detected",
                severity="warn",
                summary=f"Add-to-cart blocked by sign-in wall on {profile.name}",
                payload=signin,
            )
            run.status = RunStatus.NEEDS_USER
            run.failure_reason = (
                f"Please sign in to {profile.name} in this browser tab, then "
                f"approve the retry checkpoint."
            )
            _set_checkpoint(
                run,
                Checkpoint(
                    action="retry_add_to_cart",
                    label=f"Sign in to {profile.name} first",
                    detail=(
                        f"The add-to-cart button isn't visible because "
                        f"{profile.name} requires you to sign in. Open the tab, "
                        f"sign in, then approve this checkpoint to retry."
                    ),
                    candidate_id=run.selected_candidate_id,
                    metadata={"signin": signin},
                ),
            )
            return

        await _log_event(
            db,
            run,
            event_type="add_to_cart_failed",
            severity="error",
            summary="Could not find a clickable Add-to-cart button",
            payload={"signin": signin},
        )
        run.status = RunStatus.NEEDS_USER
        run.failure_reason = (
            "Could not find an Add-to-cart button. The page may show a variant "
            "picker, region selector, or anti-bot interstitial. Open the tab to "
            "inspect, then start a new run."
        )
        _set_checkpoint(
            run,
            Checkpoint(
                action="manual_intervention",
                label="Add to cart failed",
                detail=run.failure_reason,
                metadata={"signin": signin},
            ),
        )
        return

    # Snapshot the cart to give the user a clear summary.
    if profile.cart_url:
        try:
            await bc.navigate(executor, profile.cart_url)
        except Exception as exc:  # pragma: no cover
            logger.debug("cart navigation failed: %s", exc)

    cart = await bc.extract_cart_summary(executor)
    await _log_event(
        db,
        run,
        event_type="cart_summary",
        summary=f"Cart shows {cart.get('line_count', 0)} line(s)",
        payload=cart,
    )

    run.status = RunStatus.IN_CART
    _set_checkpoint(
        run,
        Checkpoint(
            action="proceed_to_checkout",
            label="Confirm checkout",
            detail=(
                "The item has been added to your cart. Review the cart summary "
                "below and approve to proceed to the checkout review page. The "
                "agent will stop *before* any payment or place-order button — "
                "the final order must be placed by you in the browser."
            ),
            metadata={"cart": cart},
        ),
    )
    run.status = RunStatus.AWAITING_CHECKOUT_CONFIRMATION


async def _execute_proceed_to_checkout(
    db: AsyncSession,
    run: EcommerceAgentRun,
    executor: bc.BrowserExec,
) -> None:
    """Click into the checkout funnel but stop at the payment boundary."""
    profile = get_profile(run.site_slug)
    labels = list(profile.checkout_labels) or ["checkout"]
    clicked: dict[str, Any] = {"clicked": False}
    for label in labels:
        if is_forbidden_label(label, profile):
            await _log_event(
                db,
                run,
                event_type="policy_blocked",
                severity="error",
                summary=f"Refused to click checkout label {label!r} (forbidden)",
                payload={"label": label},
            )
            continue
        try:
            result = await bc.click_by_text(executor, label)
        except Exception as exc:
            await _log_event(
                db,
                run,
                event_type="click_failed",
                severity="warn",
                summary=f"click_by_text({label!r}) failed: {exc}",
            )
            continue
        if result.get("clicked"):
            clicked = result
            await _log_event(
                db,
                run,
                event_type="checkout_click",
                summary=f"Clicked checkout label {label!r}",
                payload={"label": label, "result": result},
            )
            break

    boundary = await bc.detect_purchase_boundary(executor, profile)
    run.status = RunStatus.AT_CHECKOUT_BOUNDARY
    run.completed_at = datetime.now(UTC)
    _set_checkpoint(
        run,
        Checkpoint(
            action="final_purchase",
            label="Checkout boundary reached",
            detail=(
                "The agent has paused at the checkout review screen. Any final "
                "order/payment buttons remain visible for you to inspect — the "
                "agent will not click them."
            ),
            metadata={
                "boundary": boundary,
                "checkout_clicked": clicked,
            },
        ),
    )
    await _log_event(
        db,
        run,
        event_type="checkout_boundary_reached",
        summary="Agent stopped at checkout review",
        payload={"boundary": boundary},
    )


async def _fail_run(
    db: AsyncSession,
    run: EcommerceAgentRun,
    reason: str,
    *,
    event_type: str = "run_failed",
) -> None:
    run.status = RunStatus.FAILED
    run.failure_reason = reason
    run.completed_at = datetime.now(UTC)
    _set_checkpoint(run, None)
    await _log_event(db, run, event_type=event_type, severity="error", summary=reason)
    run.updated_at = datetime.now(UTC)
    await db.commit()


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


async def get_run(db: AsyncSession, run_id: str) -> EcommerceAgentRun | None:
    result = await db.execute(
        select(EcommerceAgentRun).where(EcommerceAgentRun.id == run_id)
    )
    return result.scalar_one_or_none()


async def list_candidates(
    db: AsyncSession, run_id: str
) -> list[EcommerceProductCandidate]:
    result = await db.execute(
        select(EcommerceProductCandidate)
        .where(EcommerceProductCandidate.run_id == run_id)
        .order_by(EcommerceProductCandidate.rank.asc())
    )
    return list(result.scalars().all())


async def list_events(db: AsyncSession, run_id: str) -> list[EcommerceAgentEvent]:
    result = await db.execute(
        select(EcommerceAgentEvent)
        .where(EcommerceAgentEvent.run_id == run_id)
        .order_by(EcommerceAgentEvent.timestamp.asc())
    )
    return list(result.scalars().all())


async def _get_candidate(
    db: AsyncSession,
    run: EcommerceAgentRun,
    candidate_id: str,
) -> EcommerceProductCandidate:
    result = await db.execute(
        select(EcommerceProductCandidate).where(
            EcommerceProductCandidate.id == candidate_id,
            EcommerceProductCandidate.run_id == run.id,
        )
    )
    candidate = result.scalar_one_or_none()
    if candidate is None:
        raise ValueError(f"Candidate {candidate_id!r} not found for run {run.id!r}")
    return candidate


def _safe_float(value: str) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_number(value: int | float | None) -> str:
    if value is None:
        return ""
    return str(value)


__all__ = [
    "Checkpoint",
    "MAX_CANDIDATES",
    "RunStatus",
    "auto_advance_after_search",
    "cancel_run",
    "confirm",
    "create_run",
    "get_checkpoint",
    "get_run",
    "list_candidates",
    "list_events",
    "search",
    "select_candidate",
]


# Keep the executor type re-exported for type hints elsewhere.
BrowserExec = bc.BrowserExec
ExecutorFactory = Callable[[], Awaitable[bc.BrowserExec]]
