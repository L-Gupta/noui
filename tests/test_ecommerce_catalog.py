"""Tests for the e-commerce site catalog and per-site safety metadata."""

from __future__ import annotations

import sys
from pathlib import Path

_NOUI_ROOT = Path(__file__).resolve().parent.parent
if str(_NOUI_ROOT) not in sys.path:
    sys.path.insert(0, str(_NOUI_ROOT))

import pytest

from backend.ecommerce.site_catalog import (
    COMMON_FORBIDDEN_LABELS,
    all_profiles,
    all_slugs,
    find_profile_by_url,
    get_profile,
    is_forbidden_label,
    normalize_label,
)

# ---------------------------------------------------------------------------
# Catalog completeness
# ---------------------------------------------------------------------------


def test_catalog_has_twenty_sites():
    profiles = all_profiles()
    assert len(profiles) == 20
    assert len({p.slug for p in profiles}) == 20  # no duplicate slugs


def test_every_profile_has_required_fields():
    for profile in all_profiles():
        assert profile.slug == profile.slug.lower()
        assert profile.domains, f"{profile.slug}: domains is empty"
        assert profile.home_url.startswith("http"), f"{profile.slug}: home_url invalid"
        assert "{query}" in profile.search_url_template, (
            f"{profile.slug}: search_url_template missing {{query}}"
        )
        # Every profile must define at least one add-to-cart label so the
        # agent has a fallback action when the catalog selectors miss.
        assert profile.add_to_cart_labels, f"{profile.slug}: no add_to_cart_labels"


def test_build_search_url_quotes_query():
    profile = get_profile("amazon")
    url = profile.build_search_url("USB C cable")
    assert "USB+C+cable" in url or "USB%20C%20cable" in url
    assert url.startswith("https://www.amazon.com/s?k=")


def test_build_search_url_rejects_empty_query():
    profile = get_profile("amazon")
    with pytest.raises(ValueError):
        profile.build_search_url("")


# ---------------------------------------------------------------------------
# Domain lookup
# ---------------------------------------------------------------------------


def test_find_profile_by_url_matches_primary_domain():
    p = find_profile_by_url("https://www.amazon.com/dp/B000ABC")
    assert p is not None and p.slug == "amazon"


def test_find_profile_by_url_matches_regional_domain():
    p = find_profile_by_url("https://www.amazon.co.uk/dp/B000XYZ")
    assert p is not None and p.slug == "amazon"


def test_find_profile_by_url_unknown_returns_none():
    assert find_profile_by_url("https://example.com/foo") is None


def test_find_profile_handles_bare_host():
    p = find_profile_by_url("ebay.com")
    assert p is not None and p.slug == "ebay"


# ---------------------------------------------------------------------------
# Forbidden label detection
# ---------------------------------------------------------------------------


def test_normalize_label_collapses_whitespace():
    assert normalize_label("  Place\nYour   Order  ") == "place your order"


@pytest.mark.parametrize(
    "label",
    [
        "Place Your Order",
        "Place order now",
        "Pay now",
        "Buy now",
        "Confirm and Pay",
        "Submit Order",
        "Complete Purchase",
    ],
)
def test_universal_forbidden_labels_block_clicks(label):
    assert is_forbidden_label(label) is True


def test_site_specific_forbidden_label_blocks():
    flipkart = get_profile("flipkart")
    # "Place Order" appears on Flipkart's address step — must still be forbidden.
    assert is_forbidden_label("PLACE ORDER", flipkart)


def test_localized_forbidden_label_blocks():
    rakuten = get_profile("rakuten")
    assert is_forbidden_label("注文を確定する", rakuten)


def test_benign_label_is_not_forbidden():
    profile = get_profile("amazon")
    # "Add to cart" must remain allowed; ranking depends on it.
    assert is_forbidden_label("Add to cart", profile) is False
    assert is_forbidden_label("Proceed to checkout", profile) is False


def test_common_forbidden_labels_are_unique_and_lowercase():
    seen = set()
    for label in COMMON_FORBIDDEN_LABELS:
        assert label == label.lower(), f"label not lowercase: {label!r}"
        assert label not in seen, f"duplicate label: {label!r}"
        seen.add(label)


# ---------------------------------------------------------------------------
# Slug lookup
# ---------------------------------------------------------------------------


def test_get_profile_unknown_slug_raises():
    with pytest.raises(KeyError):
        get_profile("definitely-not-a-real-site")


def test_all_slugs_match_catalog():
    slugs = all_slugs()
    profiles = all_profiles()
    assert slugs == tuple(p.slug for p in profiles)
