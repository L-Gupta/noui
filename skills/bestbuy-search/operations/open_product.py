#!/usr/bin/env python3
"""Open a Best Buy product detail page in the active browser tab.

Navigates to the product and waits for its key UI elements to load so
subsequent add_to_cart calls can immediately interact with the page.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from noui_runtime import browser  # noqa: E402

BESTBUY_HOST = "https://www.bestbuy.com"

# Selectors that indicate a PDP has finished rendering.
_PDP_READY_SELECTORS = [
    "button.add-to-cart-button",
    "[data-button-state='ADD_TO_CART']",
    ".fulfillment-add-to-cart-button",
    "button[class*='add-to-cart']",
]

# Fallback: wait for the product title heading.
_TITLE_SELECTOR = "h1.heading-5, h1[class*='sku-title'], h1"


async def execute(pdp_url: str) -> dict:
    """Navigate to a Best Buy product page and wait for it to be ready.

    Args:
        pdp_url: Either a relative path from lookup_products (e.g.
            "/product/lenovo-ideapad.../JJGSH82JL5") or a full URL.

    Returns:
        ``{"ready": bool, "url": str, "title": str, "add_to_cart_visible": bool}``
    """
    if pdp_url.startswith("/"):
        url = f"{BESTBUY_HOST}{pdp_url}"
    elif not pdp_url.startswith("http"):
        url = f"{BESTBUY_HOST}/{pdp_url.lstrip('/')}"
    else:
        url = pdp_url

    await browser.navigate(url)

    # Wait for the URL to settle on bestbuy.com (handles any redirect).
    await browser.wait_for_url("bestbuy.com", timeout_ms=15000)

    # Try to detect that the add-to-cart button or page title loaded.
    add_to_cart_visible = False
    for sel in _PDP_READY_SELECTORS:
        result = await browser.wait_for_selector(sel, timeout_ms=8000)
        if result.get("found"):
            add_to_cart_visible = True
            break

    if not add_to_cart_visible:
        # Fall back to waiting for the heading at minimum.
        await browser.wait_for_selector(_TITLE_SELECTOR, timeout_ms=8000)

    page = await browser.get_page_info()
    return {
        "ready": True,
        "url": page.get("url", url),
        "title": page.get("title", ""),
        "add_to_cart_visible": add_to_cart_visible,
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="open_product",
        description="Open a Best Buy product detail page in the active browser tab.",
    )
    p.add_argument(
        "--pdp-url",
        dest="pdp_url",
        required=True,
        help='Relative pdpUrl from lookup_products (e.g. "/product/.../SKU") or full URL.',
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = asyncio.run(execute(pdp_url=args.pdp_url))
    except Exception as exc:
        print(f"open_product failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
