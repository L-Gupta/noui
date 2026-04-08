"""Tests for backend/elicitation/har_analyzer.py"""

from __future__ import annotations

from datetime import UTC, datetime

from backend.elicitation.har_analyzer import (
    _auto_name_api,
    _detect_api_prefix,
    _find_nearby_clicks,
    _get_content_type,
    _is_api_call,
    _to_naive_utc,
    annotate_har_entries,
    detect_api_groups,
    filter_har_entries,
)

# ── HAR helpers ───────────────────────────────────────────────────────────────


def _make_entry(
    url: str = "https://api.example.com/v1/items",
    method: str = "GET",
    resp_content_type: str = "application/json",
    status: int = 200,
    resp_text: str = "",
    req_headers: list[dict] | None = None,
) -> dict:
    return {
        "request": {
            "method": method,
            "url": url,
            "headers": req_headers or [],
            "queryString": [],
        },
        "response": {
            "status": status,
            "content": {
                "mimeType": resp_content_type,
                "text": resp_text,
            },
            "headers": [],
        },
        "startedDateTime": "2024-01-01T12:00:00.000Z",
    }


def _make_har(entries: list[dict]) -> dict:
    return {"log": {"entries": entries}}


# ── filter_har_entries ────────────────────────────────────────────────────────


class TestFilterHarEntries:
    def test_empty_har_returns_empty(self) -> None:
        assert filter_har_entries({}) == []
        assert filter_har_entries({"log": {"entries": []}}) == []

    def test_options_skipped(self) -> None:
        entry = _make_entry(method="OPTIONS")
        result = filter_har_entries(_make_har([entry]))
        assert result == []

    def test_js_asset_skipped(self) -> None:
        entry = _make_entry(
            url="https://cdn.example.com/bundle.js",
            resp_content_type="application/javascript",
        )
        assert filter_har_entries(_make_har([entry])) == []

    def test_css_asset_skipped(self) -> None:
        entry = _make_entry(
            url="https://cdn.example.com/style.css",
            resp_content_type="text/css",
        )
        assert filter_har_entries(_make_har([entry])) == []

    def test_image_skipped(self) -> None:
        for ext in (".png", ".jpg", ".svg", ".ico", ".webp"):
            entry = _make_entry(url=f"https://cdn.example.com/img{ext}")
            assert filter_har_entries(_make_har([entry])) == [], ext

    def test_analytics_url_skipped(self) -> None:
        for frag in ("analytics", "hotjar", "segment", "sentry", "newrelic"):
            entry = _make_entry(url=f"https://cdn.example.com/{frag}/track")
            assert filter_har_entries(_make_har([entry])) == [], frag

    def test_json_api_included(self) -> None:
        entry = _make_entry(url="https://api.example.com/v1/users")
        result = filter_har_entries(_make_har([entry]))
        assert len(result) == 1
        assert result[0]["_original_index"] == 0

    def test_multiple_entries_filtered(self) -> None:
        entries = [
            _make_entry(url="https://api.example.com/v1/users"),
            _make_entry(url="https://cdn.example.com/app.js", resp_content_type="text/javascript"),
            _make_entry(url="https://api.example.com/v1/posts"),
        ]
        result = filter_har_entries(_make_har(entries))
        assert len(result) == 2
        assert result[0]["_original_index"] == 0
        assert result[1]["_original_index"] == 2

    def test_exclude_pattern_removes_entry(self) -> None:
        entries = [
            _make_entry(url="https://api.example.com/v1/users"),
            _make_entry(url="https://api.example.com/v1/internal"),
        ]
        result = filter_har_entries(_make_har(entries), exclude_patterns=[r"/internal"])
        assert len(result) == 1
        assert "users" in result[0]["request"]["url"]

    def test_include_pattern_forces_entry(self) -> None:
        entry = _make_entry(
            url="https://example.com/page.html",
            resp_content_type="text/html",
        )
        result = filter_har_entries(_make_har([entry]), include_patterns=[r"page\.html"])
        assert len(result) == 1

    def test_post_to_api_path_included(self) -> None:
        entry = _make_entry(
            url="https://example.com/api/submit",
            method="POST",
            resp_content_type="",
        )
        result = filter_har_entries(_make_har([entry]))
        assert len(result) == 1


# ── _is_api_call ──────────────────────────────────────────────────────────────


class TestIsApiCall:
    def test_json_response_is_api(self) -> None:
        entry = _make_entry(resp_content_type="application/json")
        assert _is_api_call(entry) is True

    def test_html_is_not_api(self) -> None:
        entry = _make_entry(
            url="https://example.com/page",
            resp_content_type="text/html",
        )
        assert _is_api_call(entry) is False

    def test_v1_path_is_api(self) -> None:
        entry = _make_entry(url="https://example.com/v1/resources", resp_content_type="")
        assert _is_api_call(entry) is True

    def test_graphql_path_is_api(self) -> None:
        entry = _make_entry(
            url="https://example.com/graphql",
            method="POST",
            resp_content_type="",
        )
        assert _is_api_call(entry) is True

    def test_post_with_json_body_is_api(self) -> None:
        entry = _make_entry(
            url="https://example.com/submit",
            method="POST",
            req_headers=[{"name": "Content-Type", "value": "application/json"}],
            resp_content_type="",
        )
        assert _is_api_call(entry) is True

    def test_get_with_json_response_is_api(self) -> None:
        entry = _make_entry(
            url="https://example.com/data",
            method="GET",
            resp_content_type="application/json; charset=utf-8",
        )
        assert _is_api_call(entry) is True

    def test_woff_font_is_not_api(self) -> None:
        entry = _make_entry(url="https://example.com/font.woff2")
        assert _is_api_call(entry) is False


# ── annotate_har_entries ──────────────────────────────────────────────────────


class TestAnnotateHarEntries:
    def test_step_numbers_assigned(self) -> None:
        entries = [
            _make_entry(url="https://api.example.com/v1/a"),
            _make_entry(url="https://api.example.com/v1/b"),
        ]
        result = annotate_har_entries(entries)
        assert result[0]["_annotations"]["step_number"] == 0
        assert result[1]["_annotations"]["step_number"] == 1

    def test_empty_entries_returns_empty(self) -> None:
        assert annotate_har_entries([]) == []

    def test_is_primary_default_true(self) -> None:
        entries = [_make_entry()]
        result = annotate_har_entries(entries)
        assert result[0]["_annotations"]["is_primary"] is True

    def test_related_click_events_found(self) -> None:
        entry = {
            **_make_entry(),
            "startedDateTime": "2024-01-01T12:00:02.000Z",
        }
        click = {
            "id": "c1",
            "event_type": "input",
            "field_name": "username",
            "value": "alice",
            "timestamp": "2024-01-01T12:00:01.000Z",
        }
        result = annotate_har_entries([entry], click_events=[click])
        assert "c1" in result[0]["_annotations"]["related_click_event_ids"]

    def test_click_outside_window_not_related(self) -> None:
        entry = {
            **_make_entry(),
            "startedDateTime": "2024-01-01T12:00:10.000Z",
        }
        click = {
            "id": "c2",
            "event_type": "click",
            "timestamp": "2024-01-01T12:00:00.000Z",  # 10 seconds before
        }
        result = annotate_har_entries([entry], click_events=[click])
        assert result[0]["_annotations"]["related_click_event_ids"] == []

    def test_detected_params_from_input_event(self) -> None:
        entry = {
            **_make_entry(),
            "startedDateTime": "2024-01-01T12:00:02.000Z",
        }
        click = {
            "id": "c1",
            "event_type": "input",
            "field_name": "email",
            "value": "alice@example.com",
            "timestamp": "2024-01-01T12:00:01.500Z",
        }
        result = annotate_har_entries([entry], click_events=[click])
        params = result[0]["_annotations"]["detected_params"]
        assert len(params) == 1
        assert params[0]["field_name"] == "email"
        assert params[0]["source"] == "form_input"


# ── detect_api_groups ────────────────────────────────────────────────────────


class TestDetectApiGroups:
    def test_empty_entries(self) -> None:
        assert detect_api_groups([]) == []

    def test_single_group(self) -> None:
        entries = [
            _make_entry(url="https://api.example.com/v1/users", method="GET"),
            _make_entry(url="https://api.example.com/v1/posts", method="POST"),
        ]
        groups = detect_api_groups(entries)
        assert len(groups) == 1
        assert groups[0]["entry_count"] == 2
        assert len(groups[0]["endpoints"]) == 2

    def test_two_groups(self) -> None:
        entries = [
            _make_entry(url="https://api.example.com/v1/a"),
            _make_entry(url="https://other.io/v1/b"),
        ]
        groups = detect_api_groups(entries)
        assert len(groups) == 2

    def test_sorted_by_entry_count_descending(self) -> None:
        entries = [
            _make_entry(url="https://api.example.com/v1/a"),
            _make_entry(url="https://api.example.com/v1/b"),
            _make_entry(url="https://other.io/v1/c"),
        ]
        groups = detect_api_groups(entries)
        assert groups[0]["entry_count"] >= groups[1]["entry_count"]

    def test_deduplicates_endpoints(self) -> None:
        entries = [
            _make_entry(url="https://api.example.com/v1/users", method="GET"),
            _make_entry(url="https://api.example.com/v1/users", method="GET"),
        ]
        groups = detect_api_groups(entries)
        assert len(groups[0]["endpoints"]) == 1

    def test_endpoint_structure(self) -> None:
        entries = [_make_entry(url="https://api.example.com/v1/users", method="POST")]
        groups = detect_api_groups(entries)
        ep = groups[0]["endpoints"][0]
        assert ep["method"] == "POST"
        assert "path" in ep


# ── _detect_api_prefix ────────────────────────────────────────────────────────


class TestDetectApiPrefix:
    def test_api_prefix(self) -> None:
        assert _detect_api_prefix("/api/users") == "/api"

    def test_api_v1(self) -> None:
        assert _detect_api_prefix("/api/v1/users") == "/api/v1"

    def test_v1_prefix(self) -> None:
        assert _detect_api_prefix("/v1/users") == "/v1"

    def test_v2_prefix(self) -> None:
        assert _detect_api_prefix("/v2/resources") == "/v2"

    def test_rest_prefix(self) -> None:
        assert _detect_api_prefix("/rest/endpoint") == "/rest"

    def test_no_prefix(self) -> None:
        assert _detect_api_prefix("/users/profile") == ""

    def test_root_path(self) -> None:
        assert _detect_api_prefix("/") == ""


# ── _auto_name_api ────────────────────────────────────────────────────────────


class TestAutoNameApi:
    def test_subdomain_api(self) -> None:
        assert _auto_name_api("api.example.com") == "example"

    def test_www_subdomain(self) -> None:
        assert _auto_name_api("www.example.com") == "example"

    def test_plain_domain(self) -> None:
        assert _auto_name_api("example.com") == "example"

    def test_localhost_with_port(self) -> None:
        assert _auto_name_api("localhost:8080") == "localhost"

    def test_deep_subdomain(self) -> None:
        # "crm.example.com" — 3 parts, first not "api"/"www" -> use parts[0]
        assert _auto_name_api("crm.example.com") == "crm"


# ── _to_naive_utc ─────────────────────────────────────────────────────────────


class TestToNaiveUtc:
    def test_string_with_z(self) -> None:
        dt = _to_naive_utc("2024-01-01T12:00:00.000Z")
        assert dt is not None
        assert dt.tzinfo is None

    def test_string_with_offset(self) -> None:
        dt = _to_naive_utc("2024-01-01T13:00:00+01:00")
        assert dt is not None
        assert dt.tzinfo is None
        # 13:00+01 = 12:00 UTC
        assert dt.hour == 12

    def test_naive_datetime_passthrough(self) -> None:
        naive = datetime(2024, 1, 1, 12, 0, 0)
        result = _to_naive_utc(naive)
        assert result == naive

    def test_aware_datetime_converted(self) -> None:
        aware = datetime(2024, 1, 1, 13, 0, 0, tzinfo=UTC)
        result = _to_naive_utc(aware)
        assert result is not None
        assert result.tzinfo is None
        assert result.hour == 13

    def test_non_datetime_returns_none(self) -> None:
        assert _to_naive_utc(12345) is None  # type: ignore[arg-type]
        assert _to_naive_utc(None) is None  # type: ignore[arg-type]


# ── _get_content_type ─────────────────────────────────────────────────────────


class TestGetContentType:
    def test_from_response_content(self) -> None:
        resp = {"content": {"mimeType": "application/json"}}
        assert _get_content_type(resp) == "application/json"

    def test_from_headers(self) -> None:
        req = {"headers": [{"name": "Content-Type", "value": "application/json"}]}
        assert _get_content_type(req, key="headers") == "application/json"

    def test_missing_content_type(self) -> None:
        assert _get_content_type({}) == ""

    def test_case_insensitive_header_lookup(self) -> None:
        req = {"headers": [{"name": "content-type", "value": "text/xml"}]}
        assert _get_content_type(req, key="headers") == "text/xml"


# ── _find_nearby_clicks ───────────────────────────────────────────────────────


class TestFindNearbyClicks:
    def test_click_within_window(self) -> None:
        clicks = [
            {
                "id": "c1",
                "timestamp": "2024-01-01T12:00:01.000Z",
            }
        ]
        result = _find_nearby_clicks("2024-01-01T12:00:02.000Z", clicks)
        assert len(result) == 1

    def test_click_before_window(self) -> None:
        clicks = [{"id": "c1", "timestamp": "2024-01-01T11:59:55.000Z"}]
        result = _find_nearby_clicks("2024-01-01T12:00:02.000Z", clicks)
        assert result == []

    def test_click_after_har_entry(self) -> None:
        # Click is after the HAR entry — should not be included
        clicks = [{"id": "c1", "timestamp": "2024-01-01T12:00:03.000Z"}]
        result = _find_nearby_clicks("2024-01-01T12:00:02.000Z", clicks)
        assert result == []

    def test_invalid_har_timestamp_returns_empty(self) -> None:
        clicks = [{"id": "c1", "timestamp": "2024-01-01T12:00:01.000Z"}]
        result = _find_nearby_clicks("not-a-date", clicks)
        assert result == []
