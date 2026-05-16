#!/usr/bin/env python3
"""Add the currently open Best Buy product to the cart.

SAFETY: This operation will NOT be executed without explicit human approval.
The calling assistant MUST:
  1. Show the user the product title, URL, and price before calling this.
  2. Ask for explicit confirmation ("Should I add this to your cart?").
  3. Only call this script after the user says yes.

The script clicks the Best Buy add-to-cart button on whatever product page is
currently open in the browser, waits for confirmation that the item was added,
then navigates to the cart and returns its contents.

Requires:
  - NoUI backend running at http://localhost:8002
  - Chrome extension connected (green dot at localhost:8002)
  - Active browser tab on a Best Buy product page
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
BESTBUY_CART_URL = f"{BESTBUY_HOST}/cart"

# Best Buy add-to-cart button selectors (in priority order).
_ADD_TO_CART_SELECTORS = [
    "button.add-to-cart-button",
    "[data-button-state='ADD_TO_CART']",
    ".fulfillment-add-to-cart-button",
    "button[class*='add-to-cart']",
]

# Text labels to try if selectors miss.
_ADD_TO_CART_LABELS = ["Add to Cart", "Add to cart"]

# Selectors that appear after a successful add-to-cart.
_SUCCESS_SELECTORS = [
    # Mini-cart flyout / "Item Added" overlay
    "[data-test='mini-cart-count']",
    "[aria-label*='cart' i][aria-label*='item' i]",
    ".go-to-cart-button",
    "button.go-to-cart",
    # Order summary drawer
    "[data-component='CartButton']",
]

# Payment-boundary indicators — stop immediately if any are present.
_PAYMENT_SELECTORS = [
    "input[autocomplete='cc-number']",
    "input[name*='cardNumber' i]",
    "input[placeholder*='card number' i]",
    "[aria-label*='card number' i]",
    "[data-test*='payment' i]",
]
_PAYMENT_URL_PATTERNS = ["/checkout/r/payment", "/checkout#bb-payment", "checkout/payment"]
_FORBIDDEN_BUTTON_TEXTS = [
    "place your order",
    "place order",
    "pay now",
    "confirm order",
    "submit order",
    "buy now",
]


async def _is_at_payment_boundary() -> bool:
    """Return True if any payment-form indicators are visible."""
    page = await browser.get_page_info()
    url = page.get("url", "").lower()
    if any(p in url for p in _PAYMENT_URL_PATTERNS):
        return True
    for sel in _PAYMENT_SELECTORS:
        result = await browser.wait_for_selector(sel, timeout_ms=1000)
        if result.get("found"):
            return True
    return False


async def _cart_summary() -> dict:
    """Return a lightweight cart summary (line titles + subtotal text)."""
    js = """
return (function() {
  function text(el) { return el ? el.textContent.trim().replace(/\\s+/g,' ') : ''; }
  var rows = document.querySelectorAll('[class*="cart-item" i], [data-test*="cart-item" i]');
  var lines = [];
  rows.forEach(function(row, i) {
    if (i >= 20) return;
    var title = row.querySelector('h4, h3, [class*="item-title" i], a') || row;
    var price = row.querySelector('[class*="price" i], [data-test*="price" i]');
    lines.push({title: text(title).slice(0,120), price: text(price).slice(0,30)});
  });
  var subtotal = document.querySelector(
    '[class*="subtotal" i], [data-test*="subtotal" i], [class*="order-total" i]'
  );
  return {line_count: lines.length, lines: lines, subtotal: text(subtotal), url: location.href};
})()
"""
    raw = await browser.eval_js(js)
    # eval_js returns {"success": bool, "result": <value>} after data-unwrap.
    inner = raw.get("result") if isinstance(raw, dict) else None
    if isinstance(inner, dict):
        return inner
    # Fallback: the whole raw dict might already be the payload.
    if isinstance(raw, dict) and "line_count" in raw:
        return raw
    return {}


async def execute(skip_navigation: bool = False) -> dict:
    """Add the currently open Best Buy product to the cart.

    Args:
        skip_navigation: If True, assumes the browser is already on the product
            page and skips any additional navigation before clicking add-to-cart.
            Default False.

    Returns:
        On success::

            {
              "added": true,
              "method": "selector" | "text",
              "cart_url": "https://www.bestbuy.com/cart",
              "cart": {"line_count": N, "lines": [...], "subtotal": "$...", "url": "..."},
              "payment_boundary_triggered": false
            }

        On failure::

            {"added": false, "error": "...", "payment_boundary_triggered": bool}
    """
    # Safety: abort immediately if already on a payment page.
    if await _is_at_payment_boundary():
        return {
            "added": False,
            "error": (
                "Payment boundary detected before add-to-cart. "
                "The browser is on a payment step — the agent has stopped. "
                "Complete payment manually."
            ),
            "payment_boundary_triggered": True,
        }

    # 1. Try text-based click first — Best Buy's design system uses utility
    #    classes that change frequently, so text matching is more stable.
    clicked = False
    method = ""
    for label in _ADD_TO_CART_LABELS:
        click_result = await browser.click_by_text(label)
        if click_result.get("clicked"):
            clicked = True
            method = f"text:{label}"
            break

    # 2. Fall back to CSS selectors for older page variants.
    if not clicked:
        for sel in _ADD_TO_CART_SELECTORS:
            result = await browser.query_elements(sel, include_text=False, max_results=1)
            if result.get("count", 0) > 0:
                click_result = await browser.cdp_click(sel)
                if click_result.get("clicked"):
                    clicked = True
                    method = f"selector:{sel}"
                    break

    if not clicked:
        return {
            "added": False,
            "error": (
                "Could not find an Add-to-Cart button. The page may require "
                "selecting a size/color/variant, signing in, or the item may "
                "be out of stock. Inspect the browser tab and try again."
            ),
            "payment_boundary_triggered": False,
        }

    # 3. Wait for the cart to update (mini-cart count or success overlay).
    cart_updated = False
    for sel in _SUCCESS_SELECTORS:
        result = await browser.wait_for_selector(sel, timeout_ms=8000)
        if result.get("found"):
            cart_updated = True
            break

    # 4. Navigate to the full cart page and take a summary.
    await browser.navigate(BESTBUY_CART_URL)
    await browser.wait_for_url("/cart", timeout_ms=12000)

    # Safety: make sure we haven't landed on a payment page somehow.
    if await _is_at_payment_boundary():
        return {
            "added": clicked,
            "error": (
                "Payment boundary reached unexpectedly after add-to-cart. "
                "The agent has stopped. Complete payment manually."
            ),
            "payment_boundary_triggered": True,
        }

    cart = await _cart_summary()
    return {
        "added": True,
        "method": method,
        "cart_updated_confirmed": cart_updated,
        "cart_url": BESTBUY_CART_URL,
        "cart": cart,
        "payment_boundary_triggered": False,
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="add_to_cart",
        description=(
            "Add the currently open Best Buy product to the cart. "
            "REQUIRES HUMAN APPROVAL before running."
        ),
    )
    p.add_argument(
        "--skip-navigation",
        dest="skip_navigation",
        action="store_true",
        default=False,
        help="Skip navigation; assume the browser is already on the product page.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = asyncio.run(execute(skip_navigation=args.skip_navigation))
    except Exception as exc:
        print(f"add_to_cart failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    if not result.get("added"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
