#!/usr/bin/env python3
"""Proceed from the Best Buy cart to the checkout page and STOP at payment.

SAFETY: This operation will NOT be executed without explicit human approval.
The calling assistant MUST:
  1. Show the user the full cart summary (from add_to_cart output).
  2. Ask: "The cart contains <items> for <subtotal>. Proceed to checkout?"
  3. Only call this script after the user confirms.
  4. NEVER call this script a second time if it returns payment_boundary=True.

The script clicks the "Checkout" button from the cart, walks through any
address/fulfillment screens the user already has saved, and stops as soon as
it detects payment-entry indicators. The user must complete payment manually.

HARD STOP conditions (agent halts immediately):
  - URL contains a payment path pattern
  - A credit-card number / CVV / payment method input appears in the DOM
  - A "Place Your Order" or "Pay Now" button becomes visible

Requires:
  - NoUI backend running at http://localhost:8002
  - Chrome extension connected
  - Active browser tab on the Best Buy cart page (https://www.bestbuy.com/cart)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from noui_runtime import browser  # noqa: E402

BESTBUY_HOST = "https://www.bestbuy.com"
BESTBUY_CART_URL = f"{BESTBUY_HOST}/cart"

# Labels / selectors for "Checkout" button on the cart page.
_CHECKOUT_SELECTORS = [
    "button.checkout-buttons__checkout",
    "[data-test='checkout-button']",
    "button[class*='checkout' i]",
]
_CHECKOUT_LABELS = ["Checkout", "Go to Checkout", "Proceed to Checkout"]

# ---------------------------------------------------------------------------
# Payment boundary definitions
# ---------------------------------------------------------------------------

# URL substrings that indicate the browser has entered a payment step.
_PAYMENT_URL_PATTERNS = [
    "/checkout/r/payment",
    "/checkout#bb-payment",
    "checkout/payment",
    "/checkout/step/payment",
]

# DOM selectors indicating a payment form is visible.
_PAYMENT_DOM_SELECTORS = [
    "input[autocomplete='cc-number']",
    "input[name*='cardNumber' i]",
    "input[placeholder*='card number' i]",
    "input[placeholder*='credit card' i]",
    "[aria-label*='card number' i]",
    "[data-test*='payment-method' i]",
    "iframe[src*='payment' i]",
    "iframe[src*='creditcard' i]",
    "iframe[title*='credit card' i]",
]

# Button texts that must never be clicked.
_FORBIDDEN_BUTTON_TEXTS = [
    "place your order",
    "place order",
    "pay now",
    "confirm order",
    "submit order",
]

# Checkout sub-steps the agent is allowed to pass through automatically.
# These are address/fulfillment/review steps on bestbuy.com's
# `/checkout` flow that appear before the payment entry screen.
_SAFE_CHECKOUT_URL_PATTERNS = [
    "/checkout#bb-fulfillment",
    "/checkout#bb-contact",
    "/checkout#bb-address",
    "/checkout#bb-review",
    "/checkout/r/fulfillment",
    "/checkout/r/contact",
    "/checkout/r/review",
]


async def _detect_payment_boundary() -> dict:
    """Return info about whether we're at a payment boundary.

    Returns a dict with keys:
      - ``at_boundary``: True if payment step detected
      - ``reason``: human-readable reason
      - ``url``: current page URL
    """
    page = await browser.get_page_info()
    url = page.get("url", "").lower()

    if any(p in url for p in _PAYMENT_URL_PATTERNS):
        return {"at_boundary": True, "reason": f"payment URL pattern in {url}", "url": url}

    for sel in _PAYMENT_DOM_SELECTORS:
        result = await browser.wait_for_selector(sel, timeout_ms=800)
        if result.get("found"):
            return {"at_boundary": True, "reason": f"payment field selector {sel!r} found", "url": url}

    # Check for "Place Your Order" button text.
    forbidden_js = """
return (function() {
  var texts = ['place your order','place order','pay now','confirm order','submit order'];
  var found = null;
  document.querySelectorAll('button, [role="button"], input[type="submit"]').forEach(function(el) {
    var t = (el.textContent || el.value || el.getAttribute('aria-label') || '').trim().toLowerCase();
    if (!found && texts.some(function(f){ return t.includes(f); })) {
      found = t;
    }
  });
  return found;
})()
"""
    js_result = await browser.eval_js(forbidden_js)
    found_label = js_result.get("result") if isinstance(js_result, dict) else None
    if found_label:
        return {
            "at_boundary": True,
            "reason": f"forbidden button visible: '{found_label}'",
            "url": url,
        }

    return {"at_boundary": False, "reason": "", "url": url}


async def execute(max_steps: int = 6) -> dict:
    """Proceed from the Best Buy cart through checkout to the payment step.

    The agent clicks through safe checkout sub-steps (fulfillment, address,
    review) automatically, then stops as soon as any payment indicator is
    detected. It will NOT click any payment button or enter any credentials.

    Args:
        max_steps: Max number of "Continue" / "Next" click attempts before
            giving up to avoid looping. Default 6.

    Returns:
        On reaching payment boundary (expected success)::

            {
              "checkout_reached": true,
              "payment_boundary": true,
              "stopped_at": "...",   # reason / URL
              "steps_taken": N,
              "current_url": "...",
              "current_title": "...",
              "message": "Agent stopped at payment step. Complete payment manually."
            }

        On failure::

            {
              "checkout_reached": false,
              "payment_boundary": false,
              "error": "...",
              "steps_taken": N
            }
    """
    # Ensure we're on the cart page to start.
    page = await browser.get_page_info()
    current_url = page.get("url", "").lower()
    if "/cart" not in current_url:
        await browser.navigate(BESTBUY_CART_URL)
        await browser.wait_for_url("/cart", timeout_ms=12000)

    # Immediate payment boundary check (safety).
    boundary = await _detect_payment_boundary()
    if boundary["at_boundary"]:
        return {
            "checkout_reached": True,
            "payment_boundary": True,
            "stopped_at": boundary["reason"],
            "steps_taken": 0,
            "current_url": boundary["url"],
            "current_title": "",
            "message": "Agent stopped at payment step. Complete payment manually.",
        }

    # Click the Checkout button — try text labels first (more stable than
    # CSS selectors that change with Best Buy's design system updates).
    clicked_checkout = False
    for label in _CHECKOUT_LABELS:
        click_result = await browser.click_by_text(label)
        if click_result.get("clicked"):
            clicked_checkout = True
            break

    if not clicked_checkout:
        for sel in _CHECKOUT_SELECTORS:
            result = await browser.query_elements(sel, include_text=False, max_results=1)
            if result.get("count", 0) > 0:
                click_result = await browser.cdp_click(sel)
                if click_result.get("clicked"):
                    clicked_checkout = True
                    break

    if not clicked_checkout:
        return {
            "checkout_reached": False,
            "payment_boundary": False,
            "error": (
                "Could not find the Checkout button on the cart page. "
                "Make sure the browser is on https://www.bestbuy.com/cart."
            ),
            "steps_taken": 0,
        }

    # Wait for navigation away from the cart.
    await browser.wait_for_url("/checkout", timeout_ms=15000)
    steps_taken = 1

    # Walk through safe checkout sub-steps until payment boundary.
    # "Continue" / "Next" labels are common for sub-step progression.
    _continue_labels = ["Continue", "Next", "Continue to Review", "Review Order"]

    while steps_taken < max_steps:
        await asyncio.sleep(1.5)  # brief pause for React re-renders

        boundary = await _detect_payment_boundary()
        if boundary["at_boundary"]:
            page = await browser.get_page_info()
            return {
                "checkout_reached": True,
                "payment_boundary": True,
                "stopped_at": boundary["reason"],
                "steps_taken": steps_taken,
                "current_url": page.get("url", boundary["url"]),
                "current_title": page.get("title", ""),
                "message": (
                    "Agent stopped at payment step. "
                    "Complete payment manually in the browser, then place your order."
                ),
            }

        page = await browser.get_page_info()
        page_url = page.get("url", "").lower()

        # If URL indicates a safe sub-step, click Continue.
        on_safe_step = any(p in page_url for p in _SAFE_CHECKOUT_URL_PATTERNS)
        if not on_safe_step:
            # May have reached an unexpected page; stop safely.
            break

        advanced = False
        for label in _continue_labels:
            click_result = await browser.click_by_text(label)
            if click_result.get("clicked"):
                steps_taken += 1
                advanced = True
                break

        if not advanced:
            # No "Continue" button found — either page is still loading or
            # we need user action (MFA, address form, etc.). Stop.
            break

    # Final boundary check after loop.
    boundary = await _detect_payment_boundary()
    page = await browser.get_page_info()

    if boundary["at_boundary"]:
        return {
            "checkout_reached": True,
            "payment_boundary": True,
            "stopped_at": boundary["reason"],
            "steps_taken": steps_taken,
            "current_url": page.get("url", ""),
            "current_title": page.get("title", ""),
            "message": (
                "Agent stopped at payment step. "
                "Complete payment manually in the browser, then place your order."
            ),
        }

    # Reached max_steps or got stuck on a sub-step (e.g. address form needs input).
    return {
        "checkout_reached": True,
        "payment_boundary": False,
        "stopped_at": f"max_steps={max_steps} or stuck on sub-step",
        "steps_taken": steps_taken,
        "current_url": page.get("url", ""),
        "current_title": page.get("title", ""),
        "message": (
            "Agent stopped before reaching the payment step — "
            "the checkout form may need manual input (address, contact, etc). "
            "Complete the remaining steps manually in the browser."
        ),
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="proceed_to_checkout",
        description=(
            "Proceed from the Best Buy cart to checkout and stop at payment. "
            "REQUIRES HUMAN APPROVAL before running."
        ),
    )
    p.add_argument(
        "--max-steps",
        dest="max_steps",
        type=int,
        default=6,
        help="Max checkout sub-steps to advance through (default 6).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = asyncio.run(execute(max_steps=args.max_steps))
    except Exception as exc:
        print(f"proceed_to_checkout failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
