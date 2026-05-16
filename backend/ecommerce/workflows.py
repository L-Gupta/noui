"""Per-site workflow manifests for the e-commerce agent.

A *workflow* is a stage-by-stage description of what the agent does on a
single site. The workflow data is purely declarative — it does not import
or execute browser code. The agent core in `agent.py` reads these stages
to derive expected URL patterns, success indicators, and per-site
caveats, and the test suite uses them to verify every catalog entry has
documented coverage for the four required stages:

  - ``search``           : run a keyword search on the site
  - ``browse``           : view candidate product details (navigation+extract)
  - ``cart``             : add the selected item to cart and verify cart
  - ``checkout_prep``    : enter the checkout funnel and stop at the
                           payment boundary — no final order/payment click

Workflows are intentionally lightweight metadata. Where a site has been
HAR-recorded into a NoUI MCP server, the ``mcp_server`` field points to
the generated artifact path so an operator can swap the browser path for
the API path. Sites without recorded MCP coverage degrade to the
declarative browser flow via `SiteProfile` selectors.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.ecommerce.site_catalog import all_slugs, get_profile

WORKFLOW_STAGES: tuple[str, ...] = ("search", "browse", "cart", "checkout_prep")


@dataclass(frozen=True)
class WorkflowStage:
    """One stage of a site workflow."""

    name: str
    description: str
    expected_url_substrings: tuple[str, ...] = ()
    success_indicators: tuple[str, ...] = ()
    forbidden_in_stage: tuple[str, ...] = ()


@dataclass(frozen=True)
class SiteWorkflow:
    """Workflow manifest for one site in the catalog."""

    slug: str
    stages: tuple[WorkflowStage, ...]
    mcp_server: str = ""  # path to generated FastMCP server, if any
    skill: str = ""  # path to a Cursor-style skill, if any
    notes: str = ""
    extra_caveats: tuple[str, ...] = field(default_factory=tuple)


def _default_stages(slug: str) -> tuple[WorkflowStage, ...]:
    """Build the default four-stage workflow for a catalog slug.

    Each site profile already encodes labels for add-to-cart, cart links,
    checkout entry, and forbidden actions; the workflow stages are
    parameterized off those so a single source of truth drives both the
    runtime agent and the documentation surface.
    """
    profile = get_profile(slug)
    cart_url_hint = profile.cart_url.split("//", 1)[-1] if profile.cart_url else ""
    return (
        WorkflowStage(
            name="search",
            description=(
                f"Navigate to {profile.search_url_template} with the user's query "
                f"and extract product cards using {profile.name}'s search-page selectors."
            ),
            expected_url_substrings=("search", "q="),
            success_indicators=("product_cards >= 1",),
        ),
        WorkflowStage(
            name="browse",
            description=(
                f"Open one of the surfaced product URLs on {profile.name}, then "
                "wait for the product page to render before extracting price, "
                "title, and seller details."
            ),
            success_indicators=("product_title_visible", "price_visible"),
            forbidden_in_stage=profile.forbidden_action_labels,
        ),
        WorkflowStage(
            name="cart",
            description=(
                "Click one of the site's add-to-cart labels "
                f"({', '.join(profile.add_to_cart_labels) or 'add to cart'}), then "
                f"navigate to {profile.cart_url or 'the cart link'} and snapshot "
                "the cart contents (lines, subtotal, shipping)."
            ),
            expected_url_substrings=(cart_url_hint,) if cart_url_hint else (),
            success_indicators=("cart_line_count >= 1",),
            forbidden_in_stage=profile.forbidden_action_labels,
        ),
        WorkflowStage(
            name="checkout_prep",
            description=(
                "Click into the checkout funnel using one of the site's checkout "
                f"labels ({', '.join(profile.checkout_labels) or 'checkout'}). "
                "Run purchase-boundary detection — the agent must stop the moment "
                "any final order/payment button is visible."
            ),
            success_indicators=(
                "boundary_present == true",
                "no final click dispatched",
            ),
            forbidden_in_stage=profile.forbidden_action_labels,
        ),
    )


# Sites that have known operational caveats above and beyond the catalog notes.
_EXTRA_CAVEATS: dict[str, tuple[str, ...]] = {
    "amazon": (
        "Amazon may show a 1-Click button alongside Add-to-cart on Prime accounts. "
        "Both 'Buy now' and '1-Click' are forbidden by the catalog profile.",
        "International accounts redirect to amazon.<tld>; the agent always uses the "
        "domain returned by the search redirect.",
    ),
    "aliexpress": (
        "AliExpress aggressively gates unauthenticated automation with sliding "
        "captchas. The agent will pause for manual completion.",
    ),
    "ebay": (
        "Some listings on eBay use 'Buy It Now' as the only purchase path. "
        "The agent never auto-clicks 'Buy It Now' — it stops at the listing page "
        "and surfaces a checkpoint instead.",
    ),
    "flipkart": (
        "Flipkart confusingly uses 'Place Order' on the address step too; "
        "the agent treats every 'Place Order' label as forbidden.",
    ),
    "rakuten": (
        "Rakuten checkout copy is Japanese — confirmation buttons "
        "('注文を確定する', '購入を確定') are added to the forbidden list.",
    ),
    "taobao": (
        "Taobao and Tmall regularly require Alipay authentication and may show "
        "captcha walls early in the flow.",
    ),
    "shopee": (
        "Shopee uses dynamically generated class names; selector hits may be "
        "intermittent. Fall back to text-based heuristics where possible.",
    ),
    "jd": (
        "JD checkout copy is Chinese — '提交订单' and '立即购买' are forbidden.",
    ),
    "temu": (
        "Temu shows aggressive interstitials before checkout; the agent should "
        "pause for user dismissal.",
    ),
    "mercadolibre": (
        "Regional country domains (.ar, .mx, .com.br) all map to the same slug; "
        "search URLs may need adjusting for the buyer's country.",
    ),
    "lazada": (
        "Lazada requires login before the cart is visible; the agent stops with "
        "needs_user when redirected to the login page.",
    ),
    "coupang": (
        "Coupang's labels are localized to Korean; '결제하기' and '주문하기' are "
        "forbidden.",
    ),
}


def _build_registry() -> dict[str, SiteWorkflow]:
    registry: dict[str, SiteWorkflow] = {}
    for slug in all_slugs():
        registry[slug] = SiteWorkflow(
            slug=slug,
            stages=_default_stages(slug),
            extra_caveats=_EXTRA_CAVEATS.get(slug, ()),
            notes=get_profile(slug).notes,
        )
    return registry


_REGISTRY: dict[str, SiteWorkflow] = _build_registry()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_workflow(slug: str) -> SiteWorkflow:
    """Return the workflow for ``slug``.

    Raises:
        KeyError: When no workflow has been registered for the slug.
    """
    key = slug.lower().strip()
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise KeyError(f"No workflow registered for site slug: {slug!r}") from exc


def all_workflows() -> tuple[SiteWorkflow, ...]:
    """Return every registered workflow in catalog order."""
    return tuple(_REGISTRY[s] for s in all_slugs())


def workflow_coverage_report() -> dict[str, dict[str, bool]]:
    """Return a quick coverage report keyed by slug.

    For each catalog slug, returns a dict mapping each required stage to
    whether the workflow defines it. Used by tests to enforce that every
    site has all four stages.
    """
    report: dict[str, dict[str, bool]] = {}
    for workflow in all_workflows():
        defined = {stage.name for stage in workflow.stages}
        report[workflow.slug] = {stage: stage in defined for stage in WORKFLOW_STAGES}
    return report


__all__ = [
    "SiteWorkflow",
    "WORKFLOW_STAGES",
    "WorkflowStage",
    "all_workflows",
    "get_workflow",
    "workflow_coverage_report",
]
