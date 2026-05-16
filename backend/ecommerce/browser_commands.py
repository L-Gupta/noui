"""High-level browser commands used by the e-commerce agent.

These wrap the low-level ``execute_command`` bridge from
`backend.elicitation.browser_bridge` with the small set of agent-specific
actions described in the plan:

- ``extract_product_cards``    : scrape visible product cards
- ``extract_cart_summary``     : scrape the cart page lines + totals
- ``detect_purchase_boundary`` : find any final order/payment controls and
                                 return a forced confirmation checkpoint
- ``highlight_candidate``      : visually outline a candidate URL/text so
                                 the user can verify the agent's choice

Each function emits a single ``eval_js`` browser command. The JS payload is
intentionally small and self-contained so it works against any of the
catalog sites without site-specific bundles. Per-site selectors from
`SiteProfile` are passed in as data, never compiled into the payload.

These commands assume the active browser tab is already on the relevant
page (search results, product detail, or cart). The agent code in
`agent.py` is responsible for navigating before calling.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from backend.ecommerce.site_catalog import SiteProfile, normalize_label

logger = logging.getLogger(__name__)


# Type alias: anything that matches `execute_command(command_type, params) -> dict`
BrowserExec = Callable[[str, dict], Awaitable[dict]]


# ---------------------------------------------------------------------------
# JS payloads
# ---------------------------------------------------------------------------


# Extract candidate product cards from a search/listing page using per-site
# selectors. The JS function is parameter-free; selectors are injected via
# string interpolation before the call. The result shape is:
#   { "candidates": [ {title, url, image_url, price_text, rating_text,
#                       review_count_text, seller, shipping_text, availability} ] }
_EXTRACT_PRODUCT_CARDS_JS = """
return (function() {
  const CARDS = __CARD_SELECTORS__;
  const TITLES = __TITLE_SELECTORS__;
  const PRICES = __PRICE_SELECTORS__;
  const LINKS = __LINK_SELECTORS__;
  const LIMIT = __LIMIT__;

  function deepQueryAll(root, sel) {
    const results = [...root.querySelectorAll(sel)];
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) results.push(...deepQueryAll(el.shadowRoot, sel));
    }
    return results;
  }
  function firstMatch(card, selectors) {
    for (const sel of selectors) {
      const el = card.querySelector(sel);
      if (el) return el;
    }
    return null;
  }
  function text(el) {
    return el ? (el.textContent || el.value || '').trim().replace(/\\s+/g, ' ') : '';
  }
  function attr(el, name) {
    return el ? (el.getAttribute(name) || '') : '';
  }

  let cards = [];
  for (const sel of CARDS) {
    const found = deepQueryAll(document, sel);
    if (found.length) { cards = found; break; }
  }

  const out = [];
  for (const card of cards) {
    if (out.length >= LIMIT) break;
    const rect = card.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    const titleEl = firstMatch(card, TITLES);
    const priceEl = firstMatch(card, PRICES);
    const linkEl = firstMatch(card, LINKS) || card.querySelector('a[href]');
    const imgEl = card.querySelector('img');
    const ratingEl = card.querySelector(
      "[aria-label*='star' i], [class*='rating' i], [data-test*='rating' i]"
    );
    const reviewsEl = card.querySelector(
      "[class*='review' i], [aria-label*='review' i]"
    );
    const sellerEl = card.querySelector("[class*='seller' i], [data-test*='seller' i]");
    const shippingEl = card.querySelector(
      "[class*='shipping' i], [class*='delivery' i], [data-test*='fulfillment' i]"
    );
    const availabilityEl = card.querySelector(
      "[class*='availability' i], [class*='stock' i]"
    );
    const href = linkEl ? (linkEl.href || attr(linkEl, 'href')) : '';
    out.push({
      title: text(titleEl) || text(card).slice(0, 200),
      url: href || '',
      image_url: imgEl ? (imgEl.src || imgEl.getAttribute('data-src') || '') : '',
      price_text: text(priceEl),
      rating_text: text(ratingEl) || attr(ratingEl, 'aria-label'),
      review_count_text: text(reviewsEl),
      seller: text(sellerEl),
      shipping_text: text(shippingEl),
      availability: text(availabilityEl),
    });
  }
  return { candidates: out, total_cards: cards.length };
})()
"""


# Extract the cart contents. Cart pages vary wildly; we use generic
# selectors plus a fallback that scans for visible currency text near
# product-like rows. We also surface every visible checkout-style button
# so the agent can verify it has not entered a payment funnel yet.
_EXTRACT_CART_SUMMARY_JS = """
return (function() {
  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden';
  }
  function text(el) {
    return el ? (el.textContent || '').trim().replace(/\\s+/g, ' ') : '';
  }

  const rowSelectors = [
    "[data-testid*='cart' i] li",
    "[class*='cart-item' i]",
    "[class*='cartItem' i]",
    "[data-component*='cart' i] li",
    "li[class*='line-item' i]",
  ];
  const rows = [];
  for (const sel of rowSelectors) {
    document.querySelectorAll(sel).forEach((el) => {
      if (visible(el)) rows.push(el);
    });
    if (rows.length) break;
  }

  const lines = rows.slice(0, 30).map((row, i) => {
    const link = row.querySelector('a[href]');
    const img = row.querySelector('img');
    return {
      index: i,
      title: text(row.querySelector("[class*='title' i], h2, h3, a")) || text(row).slice(0, 200),
      url: link ? (link.href || '') : '',
      image_url: img ? (img.src || img.getAttribute('data-src') || '') : '',
      price_text: text(row.querySelector("[class*='price' i], [data-test*='price' i]")),
      quantity_text: text(row.querySelector(
        "input[type='number'], [class*='qty' i], [class*='quantity' i]"
      )),
    };
  });

  const totalEl = document.querySelector(
    "[class*='subtotal' i], [data-test*='subtotal' i], [class*='order-total' i]"
  );
  const shippingEl = document.querySelector("[class*='shipping' i][class*='total' i]");
  const taxEl = document.querySelector("[class*='tax' i][class*='total' i]");

  const buttons = [];
  document.querySelectorAll(
    "button, a, [role='button'], input[type='submit'], input[type='button']"
  ).forEach((el) => {
    if (!visible(el)) return;
    const t = text(el) || el.value || el.getAttribute('aria-label') || '';
    if (!t) return;
    buttons.push({
      label: t.slice(0, 100),
      tag: el.tagName.toLowerCase(),
      disabled: el.disabled === true,
    });
  });

  return {
    line_count: lines.length,
    lines,
    subtotal_text: text(totalEl),
    shipping_text: text(shippingEl),
    tax_text: text(taxEl),
    buttons: buttons.slice(0, 50),
    url: location.href,
  };
})()
"""


# Detect any visible button/link that looks like a final order/payment
# action. The JS does NOT click anything. Forbidden phrases are passed in
# so the same code works in any locale we extend the catalog to.
_DETECT_PURCHASE_BOUNDARY_JS = """
return (function() {
  const FORBIDDEN = __FORBIDDEN_LABELS__;

  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden';
  }
  function text(el) {
    return ((el.textContent || el.value || el.getAttribute('aria-label') || '')
      .trim().replace(/\\s+/g, ' ')).toLowerCase();
  }

  const els = document.querySelectorAll(
    "button, a, [role='button'], input[type='submit'], input[type='button']"
  );
  const hits = [];
  for (const el of els) {
    if (!visible(el)) continue;
    const t = text(el);
    if (!t) continue;
    for (const needle of FORBIDDEN) {
      if (t.includes(needle)) {
        hits.push({
          label: t.slice(0, 100),
          tag: el.tagName.toLowerCase(),
          matched: needle,
        });
        break;
      }
    }
    if (hits.length >= 20) break;
  }

  return {
    url: location.href,
    boundary_present: hits.length > 0,
    matches: hits,
  };
})()
"""


# Generic, site-agnostic product-card extractor. Used as a fallback when
# the profile's selectors yield zero cards (markup drift on the live site).
# Strategy: walk every link with non-trivial visible text, find its nearest
# "card-like" ancestor, and only keep it if the ancestor also contains a
# price-looking string (currency glyph followed by digits). This works
# across the catalog without site-specific rules.
_EXTRACT_PRODUCT_CARDS_GENERIC_JS = """
return (function() {
  const LIMIT = __LIMIT__;
  const PRICE_RE = /(?:[$€£¥₹]|USD|EUR|GBP|JPY|INR|CAD|AUD)\\s?\\d/i;
  const MIN_TITLE_LEN = 12;

  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden';
  }
  function text(el) {
    return el ? (el.textContent || '').trim().replace(/\\s+/g, ' ') : '';
  }

  const seen = new Set();
  const cards = [];
  const links = document.querySelectorAll('a[href]');
  for (const link of links) {
    if (cards.length >= LIMIT) break;
    if (!visible(link)) continue;
    const title = text(link);
    if (title.length < MIN_TITLE_LEN) continue;
    if (/^(sign in|log in|register|sign up|menu|skip to|home|cart|view all)/i.test(title)) continue;
    const href = link.href || '';
    if (!href || href.startsWith('javascript:') || href.startsWith('#')) continue;
    const card = link.closest(
      'li, article, [data-asin], [data-testid], [data-component-type], [data-product-id], div[class*="card" i], div[class*="result" i], div[class*="product" i]'
    ) || link.parentElement;
    if (!card || !visible(card)) continue;
    const cardKey = card.outerHTML.slice(0, 200);
    if (seen.has(cardKey)) continue;
    const cardText = text(card);
    if (!PRICE_RE.test(cardText)) continue;
    seen.add(cardKey);

    const priceMatch = cardText.match(
      /((?:[$€£¥₹]|USD|EUR|GBP|JPY|INR|CAD|AUD)\\s?[\\d.,]+)/i
    );
    const img = card.querySelector('img');
    const ratingEl = card.querySelector(
      "[aria-label*='star' i], [class*='rating' i], [data-test*='rating' i]"
    );
    const reviewsEl = card.querySelector(
      "[class*='review' i], [aria-label*='review' i]"
    );
    cards.push({
      title: title.slice(0, 300),
      url: href,
      image_url: img ? (img.src || img.getAttribute('data-src') || '') : '',
      price_text: priceMatch ? priceMatch[1] : '',
      rating_text: ratingEl ? (text(ratingEl) || ratingEl.getAttribute('aria-label') || '') : '',
      review_count_text: text(reviewsEl),
      seller: '',
      shipping_text: '',
      availability: '',
    });
  }

  return { candidates: cards, total_cards: cards.length, source: 'generic' };
})()
"""


# Detect sign-in walls on a product page. We look for both visible buttons
# labeled "sign in / log in" AND for an absence of typical add-to-cart
# controls. Returns confidence hints rather than a hard yes/no so the agent
# can choose how to react.
_DETECT_SIGNIN_WALL_JS = """
return (function() {
  const ADD_TO_CART_HINTS = __ADD_TO_CART_HINTS__;

  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden';
  }
  function text(el) {
    return ((el.textContent || el.value || el.getAttribute('aria-label') || '')
      .trim().replace(/\\s+/g, ' ')).toLowerCase();
  }

  const SIGNIN_RE = /\\b(sign[- ]?in|log[- ]?in|continue with apple|continue with google|create account|registrieren|anmelden|inicia sesión|s'?identifier|登录|登入|로그인)\\b/i;
  const PASSWORD_FIELD = document.querySelector("input[type='password']");

  let signinControls = 0;
  let addToCartControls = 0;
  const signinHits = [];
  const els = document.querySelectorAll(
    "button, a, [role='button'], input[type='submit'], input[type='button']"
  );
  for (const el of els) {
    if (!visible(el)) continue;
    const t = text(el);
    if (!t) continue;
    if (SIGNIN_RE.test(t)) {
      signinControls += 1;
      if (signinHits.length < 5) signinHits.push(t.slice(0, 80));
    }
    for (const hint of ADD_TO_CART_HINTS) {
      if (t.includes(hint)) { addToCartControls += 1; break; }
    }
  }

  const title = (document.title || '').toLowerCase();
  const titleHint = /sign[- ]?in|log[- ]?in|登录|anmelden/i.test(title);

  return {
    signin_present: !!PASSWORD_FIELD || signinControls >= 1 && addToCartControls === 0,
    signin_controls: signinControls,
    addtocart_controls: addToCartControls,
    has_password_field: !!PASSWORD_FIELD,
    title_hint: titleHint,
    hits: signinHits,
    url: location.href,
  };
})()
"""


# Outline the element matching a given URL or title so the user can verify
# the agent has not picked the wrong row. Returns whether the highlight
# succeeded so the agent can degrade gracefully when the element scrolled
# off the DOM.
_HIGHLIGHT_CANDIDATE_JS = """
return (function() {
  const TARGET_URL = __TARGET_URL__;
  const TARGET_TEXT = __TARGET_TEXT__;

  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden';
  }

  let match = null;
  if (TARGET_URL) {
    for (const a of document.querySelectorAll('a[href]')) {
      if (a.href === TARGET_URL && visible(a)) { match = a; break; }
    }
  }
  if (!match && TARGET_TEXT) {
    const lower = TARGET_TEXT.toLowerCase();
    for (const el of document.querySelectorAll('a, h2, h3, [role="link"]')) {
      if (!visible(el)) continue;
      const t = (el.textContent || '').trim().toLowerCase();
      if (t && t.includes(lower)) { match = el; break; }
    }
  }
  if (!match) return { highlighted: false };

  let card = match.closest('article, li, div[data-testid], div[data-asin]') || match;
  card.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
  const prevOutline = card.style.outline;
  const prevOffset = card.style.outlineOffset;
  card.style.outline = '3px solid #ff6a00';
  card.style.outlineOffset = '2px';
  setTimeout(() => {
    card.style.outline = prevOutline;
    card.style.outlineOffset = prevOffset;
  }, 6000);

  const rect = card.getBoundingClientRect();
  return {
    highlighted: true,
    tag: card.tagName.toLowerCase(),
    rect: { x: Math.round(rect.x), y: Math.round(rect.y),
            width: Math.round(rect.width), height: Math.round(rect.height) },
  };
})()
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inject_payload(code: str, **values: Any) -> str:
    """Replace ``__NAME__`` placeholders with JSON-encoded values.

    Using JSON encoding means we never need to worry about quoting/escaping
    user-controlled strings (URLs, titles, localized labels) inside the JS
    payload.
    """
    rendered = code
    for name, value in values.items():
        placeholder = f"__{name}__"
        if placeholder not in rendered:
            raise ValueError(f"placeholder {placeholder} not found in JS payload")
        rendered = rendered.replace(placeholder, json.dumps(value))
    return rendered


def _unwrap_eval_result(result: dict) -> dict:
    """Extract the JS return value from an `eval_js` response.

    The browser bridge wraps results as
    ``{"success": True, "data": {"success": True, "result": <value>}, ...}``.
    """
    if not isinstance(result, dict):
        return {}
    data = result.get("data") if "success" in result else result
    if isinstance(data, dict) and "result" in data:
        inner = data["result"]
        if isinstance(inner, dict):
            return inner
    if isinstance(data, dict):
        return data
    return {}


# ---------------------------------------------------------------------------
# Public API — each function takes a `BrowserExec` so the agent can inject a
# mock in unit tests.
# ---------------------------------------------------------------------------


async def navigate(executor: BrowserExec, url: str) -> dict:
    """Navigate the active tab to ``url``."""
    return await executor("navigate", {"url": url})


def _unwrap_data(raw: dict) -> dict:
    data = raw.get("data") if isinstance(raw, dict) else None
    return data if isinstance(data, dict) else (raw if isinstance(raw, dict) else {})


async def get_page_info(executor: BrowserExec) -> dict:
    """Return the active tab's URL/title via the existing extension command."""
    raw = await executor("get_page_info", {})
    return _unwrap_data(raw)


async def extract_product_cards(
    executor: BrowserExec,
    profile: SiteProfile,
    *,
    limit: int = 20,
) -> dict:
    """Scrape the active page for product cards using the site's selectors."""
    code = _inject_payload(
        _EXTRACT_PRODUCT_CARDS_JS,
        CARD_SELECTORS=list(profile.product_card_selectors),
        TITLE_SELECTORS=list(profile.product_title_selectors),
        PRICE_SELECTORS=list(profile.product_price_selectors),
        LINK_SELECTORS=list(profile.product_link_selectors),
        LIMIT=int(limit),
    )
    raw = await executor("eval_js", {"code": code})
    return _unwrap_eval_result(raw)


async def extract_cart_summary(executor: BrowserExec) -> dict:
    """Scrape the active page (assumed to be a cart page) for cart contents."""
    raw = await executor("eval_js", {"code": _EXTRACT_CART_SUMMARY_JS})
    return _unwrap_eval_result(raw)


async def detect_purchase_boundary(
    executor: BrowserExec,
    profile: SiteProfile | None,
    extra_forbidden_labels: list[str] | None = None,
) -> dict:
    """Detect any visible final-order/payment button on the active page."""
    from backend.ecommerce.site_catalog import COMMON_FORBIDDEN_LABELS

    forbidden = [normalize_label(p) for p in COMMON_FORBIDDEN_LABELS]
    if profile is not None:
        forbidden.extend(normalize_label(p) for p in profile.forbidden_action_labels)
    if extra_forbidden_labels:
        forbidden.extend(normalize_label(p) for p in extra_forbidden_labels)
    forbidden = sorted({p for p in forbidden if p})

    code = _inject_payload(_DETECT_PURCHASE_BOUNDARY_JS, FORBIDDEN_LABELS=forbidden)
    raw = await executor("eval_js", {"code": code})
    return _unwrap_eval_result(raw)


async def highlight_candidate(
    executor: BrowserExec,
    *,
    target_url: str = "",
    target_text: str = "",
) -> dict:
    """Visually outline the candidate matching ``target_url`` or ``target_text``."""
    if not target_url and not target_text:
        raise ValueError("highlight_candidate requires target_url or target_text")
    code = _inject_payload(
        _HIGHLIGHT_CANDIDATE_JS,
        TARGET_URL=target_url,
        TARGET_TEXT=target_text,
    )
    raw = await executor("eval_js", {"code": code})
    return _unwrap_eval_result(raw)


async def wait_for_selector(
    executor: BrowserExec, selector: str, *, timeout_ms: int = 10000
) -> dict:
    """Wait until ``selector`` appears in the DOM (uses extension command)."""
    raw = await executor("wait_for_selector", {"selector": selector, "timeout": timeout_ms})
    return _unwrap_data(raw)


async def wait_for_url(
    executor: BrowserExec, url_substring: str, *, timeout_ms: int = 10000
) -> dict:
    """Wait until ``window.location.href`` contains ``url_substring``."""
    raw = await executor(
        "wait_for_url", {"url_substring": url_substring, "timeout": timeout_ms}
    )
    return _unwrap_data(raw)


_BOT_CHECK_URL_RE = re.compile(
    r"(captcha|robotcheck|validateCaptcha|errors/validate|opfcaptcha|access[-_]?denied|bot[-_]?check|incident\?support)",
    re.IGNORECASE,
)
_BOT_CHECK_TITLE_RE = re.compile(
    r"(robot|captcha|verify|are\s+you\s+human|access\s+denied|security\s+check|just\s+a\s+moment)",
    re.IGNORECASE,
)


def is_bot_check(url: str, title: str = "") -> bool:
    """Heuristic: does this look like a bot-mitigation / captcha page?"""
    if url and _BOT_CHECK_URL_RE.search(url):
        return True
    return bool(title and _BOT_CHECK_TITLE_RE.search(title))


async def wait_for_page_ready(
    executor: BrowserExec,
    profile: SiteProfile,
    target_url: str,
    *,
    selector_timeout_ms: int = 8000,
    url_timeout_ms: int = 6000,
) -> dict:
    """Wait until the active tab has navigated to ``target_url`` and at least
    one of the site profile's product-card selectors is in the DOM.

    Returns ``{"ready": bool, "url": str, "title": str, "matched_selector": str,
    "bot_check_hint": bool, "waited_ms": int}``. Never raises — caller is
    responsible for interpreting ``ready``.
    """
    parsed = urlparse(target_url)
    host = parsed.netloc or ""
    # Strip a leading "www." so we match e.g. "ebay.com" against either
    # "www.ebay.com" or a bare "ebay.com".
    if host.startswith("www."):
        host = host[4:]

    url_result: dict[str, Any] = {}
    if host:
        try:
            url_result = await wait_for_url(executor, host, timeout_ms=url_timeout_ms)
        except Exception as exc:  # pragma: no cover — bridge errors surface elsewhere
            logger.debug("wait_for_url failed: %s", exc)

    # Probe each card selector in turn. The extension's wait_for_selector
    # returns immediately if the selector is already present, so this loop
    # is cheap when the page is fast and bounded when it is slow.
    matched_selector = ""
    total_waited = int(url_result.get("waited", 0) or 0)
    per_selector_budget = max(
        500, selector_timeout_ms // max(1, len(profile.product_card_selectors) or 1)
    )
    for sel in profile.product_card_selectors:
        try:
            res = await wait_for_selector(executor, sel, timeout_ms=per_selector_budget)
        except Exception as exc:  # pragma: no cover
            logger.debug("wait_for_selector(%r) failed: %s", sel, exc)
            continue
        total_waited += int(res.get("waited", 0) or 0)
        if res.get("found"):
            matched_selector = sel
            break

    info: dict[str, Any] = {}
    try:
        info = await get_page_info(executor)
    except Exception as exc:  # pragma: no cover
        logger.debug("get_page_info failed: %s", exc)
    final_url = str(info.get("url") or url_result.get("url") or "")
    page_title = str(info.get("title") or "")

    return {
        "ready": bool(matched_selector) and bool(url_result.get("matched", True)),
        "url": final_url,
        "title": page_title,
        "matched_selector": matched_selector,
        "bot_check_hint": is_bot_check(final_url, page_title),
        "waited_ms": total_waited,
    }


async def extract_product_cards_robust(
    executor: BrowserExec,
    profile: SiteProfile,
    *,
    limit: int = 20,
) -> dict:
    """Extract product cards using profile selectors, falling back to a
    site-agnostic price+link heuristic when the profile selectors miss.

    Result shape matches ``extract_product_cards`` plus a ``source`` field
    of ``"profile"`` (when site selectors matched) or ``"generic"`` (when
    the fallback found cards).
    """
    primary = await extract_product_cards(executor, profile, limit=limit)
    primary_cards = primary.get("candidates") or []
    if primary_cards:
        primary.setdefault("source", "profile")
        return primary

    code = _inject_payload(_EXTRACT_PRODUCT_CARDS_GENERIC_JS, LIMIT=int(limit))
    raw = await executor("eval_js", {"code": code})
    fallback = _unwrap_eval_result(raw)
    fallback_cards = fallback.get("candidates") or []
    fallback.setdefault("source", "generic")
    fallback["primary_total_cards"] = primary.get("total_cards", 0)
    fallback["primary_source"] = "profile"
    if not fallback_cards:
        fallback["candidates"] = []
    return fallback


async def detect_signin_wall(
    executor: BrowserExec,
    profile: SiteProfile,
) -> dict:
    """Return heuristic info on whether the current page is a sign-in wall.

    Looks for a visible password field, sign-in-shaped buttons/links, and
    whether the page has any of the profile's add-to-cart hints. Caller
    decides what to do with the result.
    """
    hints = sorted({normalize_label(p) for p in profile.add_to_cart_labels if p})
    code = _inject_payload(_DETECT_SIGNIN_WALL_JS, ADD_TO_CART_HINTS=hints)
    raw = await executor("eval_js", {"code": code})
    return _unwrap_eval_result(raw)


async def click_by_text(executor: BrowserExec, text: str, *, exact: bool = False) -> dict:
    """Click the first visible interactive element whose label contains ``text``."""
    raw = await executor("click_by_text", {"text": text, "exact": exact})
    return _unwrap_data(raw)


__all__ = [
    "BrowserExec",
    "click_by_text",
    "detect_purchase_boundary",
    "detect_signin_wall",
    "extract_cart_summary",
    "extract_product_cards",
    "extract_product_cards_robust",
    "get_page_info",
    "highlight_candidate",
    "is_bot_check",
    "navigate",
    "wait_for_page_ready",
    "wait_for_selector",
    "wait_for_url",
]
