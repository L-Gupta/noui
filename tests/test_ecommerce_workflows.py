"""Tests for the per-site workflow manifests."""

from __future__ import annotations

import sys
from pathlib import Path

_NOUI_ROOT = Path(__file__).resolve().parent.parent
if str(_NOUI_ROOT) not in sys.path:
    sys.path.insert(0, str(_NOUI_ROOT))

import pytest

from backend.ecommerce.site_catalog import all_slugs
from backend.ecommerce.workflows import (
    WORKFLOW_STAGES,
    all_workflows,
    get_workflow,
    workflow_coverage_report,
)


def test_every_catalog_slug_has_a_workflow():
    workflow_slugs = {w.slug for w in all_workflows()}
    assert workflow_slugs == set(all_slugs())


def test_every_workflow_covers_all_required_stages():
    report = workflow_coverage_report()
    for slug in all_slugs():
        for stage in WORKFLOW_STAGES:
            assert report[slug][stage], f"{slug} is missing the {stage!r} stage"


def test_workflow_stages_are_unique_within_a_site():
    for w in all_workflows():
        names = [s.name for s in w.stages]
        assert len(names) == len(set(names)), f"{w.slug} has duplicate stages"


def test_get_workflow_unknown_slug_raises():
    with pytest.raises(KeyError):
        get_workflow("not-a-site")


def test_known_caveat_sites_carry_extra_notes():
    # Sites with high-friction flows should have at least one extra caveat
    # documented so operators are not surprised at runtime.
    for slug in ("amazon", "aliexpress", "flipkart", "rakuten", "taobao"):
        w = get_workflow(slug)
        assert w.extra_caveats, f"{slug} is missing operational caveats"
