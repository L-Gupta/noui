"""Tests for the e-commerce candidate parsing and ranking helpers."""

from __future__ import annotations

import sys
from pathlib import Path

_NOUI_ROOT = Path(__file__).resolve().parent.parent
if str(_NOUI_ROOT) not in sys.path:
    sys.path.insert(0, str(_NOUI_ROOT))

import pytest

from backend.ecommerce.ranking import (
    normalize_candidate,
    parse_price,
    parse_rating,
    parse_review_count,
    rank_candidates,
    score_candidate,
)

# ---------------------------------------------------------------------------
# parse_price
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,amount,currency",
    [
        ("$1,234.56", 1234.56, "USD"),
        ("$9.99", 9.99, "USD"),
        ("€19,99", 19.99, "EUR"),
        ("£249", 249.0, "GBP"),
        ("¥1,200", 1200.0, "JPY"),
        ("₹2,499", 2499.0, "INR"),
        ("USD 49.50", 49.50, "USD"),
        ("S$ 12.40", 12.40, "SGD"),
        ("R$ 1.234,56", 1234.56, "BRL"),
    ],
)
def test_parse_price_known_formats(text, amount, currency):
    parsed = parse_price(text)
    assert parsed.amount == pytest.approx(amount)
    assert parsed.currency == currency


def test_parse_price_empty():
    parsed = parse_price("")
    assert parsed.amount is None
    assert parsed.currency == ""


def test_parse_price_unparseable():
    parsed = parse_price("free shipping")
    assert parsed.amount is None


# ---------------------------------------------------------------------------
# parse_rating / parse_review_count
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,rating",
    [
        ("4.5 out of 5 stars", 4.5),
        ("4.0 stars", 4.0),
        ("Rated 3.7", 3.7),
        ("5/5", 5.0),
    ],
)
def test_parse_rating(text, rating):
    assert parse_rating(text) == pytest.approx(rating)


def test_parse_rating_invalid_clamps_to_none():
    assert parse_rating("9999") is None
    assert parse_rating("") is None


@pytest.mark.parametrize(
    "text,count",
    [
        ("1,234 reviews", 1234),
        ("12 ratings", 12),
        ("99 review", 99),
        ("1.234 ratings", 1234),
    ],
)
def test_parse_review_count(text, count):
    assert parse_review_count(text) == count


def test_parse_review_count_empty():
    assert parse_review_count("") is None


# ---------------------------------------------------------------------------
# normalize_candidate
# ---------------------------------------------------------------------------


def test_normalize_candidate_fills_parsed_fields():
    raw = {
        "title": "  USB-C Cable  ",
        "url": "https://example.com/abc",
        "price_text": "$12.99",
        "rating_text": "4.5 out of 5 stars",
        "review_count_text": "1,234 reviews",
        "seller": "Example Inc",
        "availability": "In Stock",
    }
    out = normalize_candidate(raw, default_currency="USD")
    assert out["title"] == "USB-C Cable"
    assert out["price_amount"] == pytest.approx(12.99)
    assert out["currency"] == "USD"
    assert out["rating_value"] == pytest.approx(4.5)
    assert out["review_count"] == 1234
    assert out["seller"] == "Example Inc"


def test_normalize_candidate_falls_back_to_default_currency():
    raw = {"title": "Widget", "price_text": "999"}
    out = normalize_candidate(raw, default_currency="JPY")
    assert out["currency"] == "JPY"


# ---------------------------------------------------------------------------
# Scoring + ranking
# ---------------------------------------------------------------------------


def test_score_candidate_prefers_under_budget():
    high = {"price_amount": 95.0, "rating_value": 4.5, "review_count": 500}
    low = {"price_amount": 30.0, "rating_value": 4.5, "review_count": 500}
    assert score_candidate(low, max_price=100) > score_candidate(high, max_price=100)


def test_score_candidate_zero_when_over_budget():
    over = {"price_amount": 150.0, "rating_value": 5.0, "review_count": 1000}
    assert score_candidate(over, max_price=100) < 0.5


def test_score_candidate_respects_min_rating():
    low_rating = {"price_amount": 10.0, "rating_value": 2.0}
    assert score_candidate(low_rating, max_price=50, min_rating=4.0) < 0.5


def test_score_candidate_handles_no_signals():
    assert 0.0 <= score_candidate({}) <= 1.0


def test_rank_candidates_orders_descending():
    items = [
        {"title": "A", "price_amount": 90.0, "rating_value": 3.0, "review_count": 10},
        {"title": "B", "price_amount": 30.0, "rating_value": 4.5, "review_count": 500},
        {"title": "C", "price_amount": 50.0, "rating_value": 4.0, "review_count": 100},
    ]
    ranked = rank_candidates(items, max_price=100, min_rating=3.0)
    assert ranked[0]["title"] == "B"
    assert [c["rank"] for c in ranked] == [1, 2, 3]
    # Scores are sorted descending.
    assert ranked[0]["score"] >= ranked[1]["score"] >= ranked[2]["score"]


def test_rank_candidates_respects_min_rating():
    items = [
        {"title": "Bad", "price_amount": 5.0, "rating_value": 2.0, "review_count": 5},
        {"title": "Good", "price_amount": 25.0, "rating_value": 4.5, "review_count": 200},
    ]
    ranked = rank_candidates(items, max_price=50, min_rating=4.0)
    assert ranked[0]["title"] == "Good"
