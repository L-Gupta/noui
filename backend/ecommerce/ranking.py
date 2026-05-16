"""Pure helpers for parsing scraped product fields and ranking candidates.

Kept as pure functions so they can be unit-tested without a live browser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PRICE_RE = re.compile(
    r"(?P<currency>[A-Z]{3}|[$€£¥₹₩₪₱]|US\$|Rs\.?|S\$|R\$|HK\$|A\$|C\$)?\s*"
    r"(?P<amount>\d[\d.,]*)",
    re.IGNORECASE,
)
_RATING_RE = re.compile(r"(\d(?:\.\d+)?)\s*(?:out of|/|\s*stars?)?", re.IGNORECASE)
_REVIEWS_RE = re.compile(r"(\d[\d,.]*)\s*(?:reviews?|ratings?)", re.IGNORECASE)

# Currency symbols → ISO 4217 (best effort; some symbols are ambiguous).
_SYMBOL_TO_ISO: dict[str, str] = {
    "$": "USD",
    "US$": "USD",
    "C$": "CAD",
    "A$": "AUD",
    "S$": "SGD",
    "HK$": "HKD",
    "R$": "BRL",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₹": "INR",
    "₩": "KRW",
    "₪": "ILS",
    "₱": "PHP",
    "Rs": "INR",
    "Rs.": "INR",
}


@dataclass(frozen=True)
class ParsedPrice:
    amount: float | None
    currency: str


def parse_price(text: str) -> ParsedPrice:
    """Extract a numeric amount and (best-effort) ISO currency code.

    Empty/None input yields ``ParsedPrice(None, "")``. Unparsable numbers
    yield ``ParsedPrice(None, currency)`` so the caller can still surface
    a currency hint.
    """
    if not text:
        return ParsedPrice(None, "")
    match = _PRICE_RE.search(text)
    if not match:
        return ParsedPrice(None, "")
    raw_currency = (match.group("currency") or "").strip()
    iso = _SYMBOL_TO_ISO.get(raw_currency, "")
    if not iso and len(raw_currency) == 3 and raw_currency.isalpha():
        iso = raw_currency.upper()
    amount = _parse_amount(match.group("amount"))
    return ParsedPrice(amount, iso)


def _parse_amount(raw: str) -> float | None:
    """Parse a localized amount string into a float.

    Supports both ``1,234.56`` (en) and ``1.234,56`` (de/fr/es) shapes.
    """
    if not raw:
        return None
    raw = raw.strip()
    has_dot = "." in raw
    has_comma = "," in raw
    try:
        if has_dot and has_comma:
            # Use the rightmost separator as the decimal point.
            if raw.rfind(",") > raw.rfind("."):
                normalized = raw.replace(".", "").replace(",", ".")
            else:
                normalized = raw.replace(",", "")
            return float(normalized)
        if has_comma and not has_dot:
            # Comma might be thousands (e.g. "1,234") or decimals ("9,99").
            # Heuristic: if there are exactly two digits after the last comma
            # and no dot, treat as decimal separator.
            tail = raw.rsplit(",", 1)[-1]
            if len(tail) == 2:
                return float(raw.replace(".", "").replace(",", "."))
            return float(raw.replace(",", ""))
        return float(raw)
    except ValueError:
        return None


def parse_rating(text: str) -> float | None:
    """Best-effort extraction of a star rating from free-form text."""
    if not text:
        return None
    match = _RATING_RE.search(text)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    if 0 <= value <= 5:
        return value
    return None


def parse_review_count(text: str) -> int | None:
    """Extract a review/rating count from free-form text."""
    if not text:
        return None
    match = _REVIEWS_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", "").replace(".", ""))
    except ValueError:
        return None


def normalize_candidate(raw: dict, *, default_currency: str = "") -> dict:
    """Normalize a raw card extracted from a product grid.

    Adds parsed numeric fields without dropping the raw text fields so the
    UI can still display the seller-rendered strings.
    """
    title = (raw.get("title") or "").strip()
    url = (raw.get("url") or "").strip()
    image_url = (raw.get("image_url") or "").strip()
    price_text = (raw.get("price_text") or raw.get("price") or "").strip()
    rating_text = (raw.get("rating_text") or raw.get("rating") or "").strip()
    review_text = (raw.get("review_count_text") or raw.get("reviews") or "").strip()
    seller = (raw.get("seller") or "").strip()
    shipping = (raw.get("shipping_text") or raw.get("shipping") or "").strip()
    availability = (raw.get("availability") or "").strip()

    parsed_price = parse_price(price_text)
    currency = parsed_price.currency or default_currency
    rating = parse_rating(rating_text)
    review_count = parse_review_count(review_text)

    return {
        "title": title,
        "url": url,
        "image_url": image_url,
        "price_text": price_text,
        "price_amount": parsed_price.amount,
        "currency": currency,
        "rating_text": rating_text,
        "rating_value": rating,
        "review_count": review_count,
        "seller": seller,
        "shipping_text": shipping,
        "availability": availability,
    }


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def score_candidate(
    candidate: dict,
    *,
    max_price: float | None = None,
    min_rating: float | None = None,
) -> float:
    """Compute a ranking score in [0, 1] (higher is better).

    Heuristic combination of price-fit, rating, and review confidence.
    Missing fields receive a neutral contribution so they are not unfairly
    pushed to the bottom.

    Hard constraints (over-budget or below ``min_rating``) cut the final
    score in half so disqualified items sink below qualifying ones even
    when their other signals are strong.
    """
    parts: list[tuple[float, float]] = []
    disqualified = False

    price = candidate.get("price_amount")
    if max_price and price is not None:
        if price <= 0:
            price_score = 0.0
        elif price <= max_price:
            # Linearly reward being well under the budget, cap at 1.0.
            price_score = 1.0 - max(0.0, (price - max_price * 0.4) / (max_price * 0.6))
            price_score = max(0.0, min(1.0, price_score))
        else:
            price_score = 0.0
            disqualified = True
        parts.append((0.4, price_score))
    elif price is not None and price > 0:
        # Without an explicit budget, give a mild bonus for "has a price".
        parts.append((0.1, 0.6))

    rating = candidate.get("rating_value")
    if rating is not None:
        rating_score = max(0.0, min(1.0, rating / 5.0))
        if min_rating is not None and rating < min_rating:
            rating_score = 0.0
            disqualified = True
        parts.append((0.35, rating_score))

    reviews = candidate.get("review_count")
    if reviews is not None and reviews > 0:
        # Diminishing returns past ~1000 reviews.
        review_score = min(1.0, reviews / 1000.0)
        parts.append((0.15, review_score))

    availability = (candidate.get("availability") or "").lower()
    if availability:
        in_stock = not any(token in availability for token in ("out of stock", "unavailable", "sold out"))
        parts.append((0.1, 1.0 if in_stock else 0.0))

    if not parts:
        return 0.5

    total_weight = sum(w for w, _ in parts)
    if total_weight == 0:
        return 0.5
    base = sum(w * s for w, s in parts) / total_weight
    if disqualified:
        # Hard penalty keeps disqualified items below qualifying ones even
        # when the rest of their signals are strong.
        base *= 0.4
    return base


def rank_candidates(
    candidates: list[dict],
    *,
    max_price: float | None = None,
    min_rating: float | None = None,
) -> list[dict]:
    """Return ``candidates`` sorted by score (descending) with ``rank``/``score`` filled in."""
    scored = [
        (score_candidate(c, max_price=max_price, min_rating=min_rating), c) for c in candidates
    ]
    scored.sort(key=lambda x: x[0], reverse=True)
    ranked: list[dict] = []
    for index, (score, c) in enumerate(scored, start=1):
        ranked.append({**c, "rank": index, "score": score})
    return ranked


__all__ = [
    "ParsedPrice",
    "normalize_candidate",
    "parse_price",
    "parse_rating",
    "parse_review_count",
    "rank_candidates",
    "score_candidate",
]
