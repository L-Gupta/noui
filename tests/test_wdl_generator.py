"""Tests for backend/elicitation/wdl_generator.py"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from backend.elicitation.wdl_generator import (
    _collect_form_values,
    _detect_base_url,
    _extract_api_headers,
    _extract_scalar_values,
    _find_final_narration,
    _find_matching_narration,
    _parameterize_dict,
    _parameterize_string,
    _parse_response_body,
    _parse_to_naive_utc,
    _path_to_jq,
    _path_to_variable_name,
    _to_var_name,
    detect_dependencies,
    generate_wdl,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _entry(
    url: str = "https://api.example.com/v1/items",
    method: str = "GET",
    body_json: dict | None = None,
    resp_json: dict | None = None,
    headers: list[dict] | None = None,
    ts: str = "2024-01-01T12:00:00.000Z",
) -> dict:
    resp_text = json.dumps(resp_json) if resp_json is not None else ""
    req_body: dict = {}
    if body_json is not None:
        req_body = {"text": json.dumps(body_json)}
    return {
        "request": {
            "method": method,
            "url": url,
            "headers": headers or [],
            "postData": req_body,
            "queryString": [],
        },
        "response": {
            "status": 200,
            "content": {"mimeType": "application/json", "text": resp_text},
        },
        "startedDateTime": ts,
    }


# ── generate_wdl ─────────────────────────────────────────────────────────────


class TestGenerateWdl:
    def test_empty_entries_returns_empty_steps(self) -> None:
        result = generate_wdl([])
        assert result["steps"] == []
        assert result["detected_params"] == {}
        assert result["detected_dependencies"] == []

    def test_single_entry_produces_rest_step(self) -> None:
        entries = [_entry()]
        result = generate_wdl(entries)
        assert len(result["steps"]) >= 1
        assert result["steps"][0]["operation"] == "REST"

    def test_base_url_detected(self) -> None:
        entries = [_entry(url="https://api.example.com/v1/items")]
        result = generate_wdl(entries)
        assert "base_url" in result["detected_params"]
        assert result["detected_params"]["base_url"]["example"] == "https://api.example.com"

    def test_url_parameterized(self) -> None:
        entries = [_entry(url="https://api.example.com/v1/items")]
        result = generate_wdl(entries, base_url="https://api.example.com")
        step = result["steps"][0]
        assert step["config"]["url"].startswith("{{base_url}}")

    def test_source_stats(self) -> None:
        entries = [_entry(), _entry()]
        result = generate_wdl(entries, click_events=[{}], narrations=[{"content": "hi"}])
        assert result["source_stats"]["har_entries"] == 2

    def test_bearer_token_parameterized(self) -> None:
        entry = _entry(headers=[{"name": "Authorization", "value": "Bearer my-long-token"}])
        result = generate_wdl([entry])
        step = result["steps"][0]
        auth_header = step["config"]["headers"]["Authorization"]
        assert auth_header == "Bearer {{access_token}}"
        assert "access_token" in result["detected_params"]

    def test_api_key_parameterized(self) -> None:
        entry = _entry(headers=[{"name": "x-api-key", "value": "supersecret"}])
        result = generate_wdl([entry])
        step = result["steps"][0]
        assert step["config"]["headers"]["x-api-key"] == "{{api_key}}"

    def test_narration_added_as_description(self) -> None:
        entry = _entry(ts="2024-01-01T12:00:01.000Z")
        narration = {"content": "Click the submit button", "timestamp": "2024-01-01T12:00:01.500Z"}
        result = generate_wdl([entry], narrations=[narration])
        step = result["steps"][0]
        assert step.get("description") == "Click the submit button"

    def test_final_narration_produces_output_text(self) -> None:
        entry = _entry()
        narrations = [{"content": "The invoice was created successfully", "timestamp": None}]
        result = generate_wdl([entry], narrations=narrations)
        last_step = result["steps"][-1]
        assert last_step["operation"] == "OUTPUT_TEXT"

    def test_dependency_produces_jq_step(self) -> None:
        # First call returns id, second call uses it in URL
        e1 = _entry(url="https://api.example.com/v1/orders", resp_json={"id": "order-123"})
        e2 = _entry(url="https://api.example.com/v1/orders/order-123/items")
        result = generate_wdl([e1, e2])
        step_ops = [s["operation"] for s in result["steps"]]
        assert "JQ_FILTER" in step_ops


# ── _detect_base_url ─────────────────────────────────────────────────────────


class TestDetectBaseUrl:
    def test_single_entry(self) -> None:
        entries = [_entry(url="https://api.example.com/v1/users")]
        assert _detect_base_url(entries) == "https://api.example.com"

    def test_most_common_wins(self) -> None:
        entries = [
            _entry(url="https://api.example.com/v1/a"),
            _entry(url="https://api.example.com/v1/b"),
            _entry(url="https://other.io/v1/c"),
        ]
        assert _detect_base_url(entries) == "https://api.example.com"

    def test_empty_returns_empty_string(self) -> None:
        assert _detect_base_url([]) == ""

    def test_missing_url_skipped(self) -> None:
        entries = [{"request": {"url": ""}}]
        assert _detect_base_url(entries) == ""


# ── detect_dependencies ───────────────────────────────────────────────────────


class TestDetectDependencies:
    def test_no_deps_with_single_entry(self) -> None:
        assert detect_dependencies([_entry()]) == []

    def test_detects_id_dependency(self) -> None:
        e1 = _entry(url="https://api.example.com/v1/orders", resp_json={"id": "order-abc-123"})
        e2 = _entry(url="https://api.example.com/v1/orders/order-abc-123/items")
        deps = detect_dependencies([e1, e2])
        assert len(deps) >= 1
        assert deps[0]["from_entry"] == 0
        assert deps[0]["to_entry"] == 1

    def test_short_values_ignored(self) -> None:
        # Value "ok" is < 4 chars (_MIN_DEP_VALUE_LEN)
        e1 = _entry(resp_json={"status": "ok"})
        e2 = _entry(url="https://api.example.com/v1/ok")
        assert detect_dependencies([e1, e2]) == []

    def test_ignored_values_not_tracked(self) -> None:
        e1 = _entry(resp_json={"status": "success"})
        e2 = _entry(url="https://api.example.com/v1/success")
        deps = detect_dependencies([e1, e2])
        assert not any(d["variable"] == "status" and d["value"] == "success" for d in deps)

    def test_non_json_response_skipped(self) -> None:
        e1 = {
            "request": {
                "url": "https://example.com/a",
                "method": "GET",
                "headers": [],
                "queryString": [],
                "postData": {},
            },
            "response": {"status": 200, "content": {"text": "plain text"}},
            "startedDateTime": "2024-01-01T12:00:00Z",
        }
        e2 = _entry()
        # Should not crash
        deps = detect_dependencies([e1, e2])
        assert isinstance(deps, list)


# ── _collect_form_values ─────────────────────────────────────────────────────


class TestCollectFormValues:
    def test_input_events(self) -> None:
        clicks = [{"event_type": "input", "field_name": "email", "value": "a@b.com"}]
        result = _collect_form_values(clicks)
        assert result == {"email": "a@b.com"}

    def test_change_events(self) -> None:
        clicks = [{"event_type": "change", "field_name": "country", "value": "US"}]
        result = _collect_form_values(clicks)
        assert result == {"country": "US"}

    def test_click_events_skipped(self) -> None:
        clicks = [{"event_type": "click", "field_name": "submit", "value": "Submit"}]
        assert _collect_form_values(clicks) == {}

    def test_empty_value_skipped(self) -> None:
        clicks = [{"event_type": "input", "field_name": "name", "value": ""}]
        assert _collect_form_values(clicks) == {}

    def test_element_id_fallback(self) -> None:
        clicks = [
            {"event_type": "input", "field_name": "", "element_id": "myInput", "value": "hello"}
        ]
        result = _collect_form_values(clicks)
        assert "myInput" in result


# ── _parameterize_string ──────────────────────────────────────────────────────


class TestParameterizeString:
    def test_replaces_known_value(self) -> None:
        params: dict = {}
        result = _parameterize_string(
            "https://example.com/users/alice",
            {"username": "alice"},
            params,
        )
        assert "{{username}}" in result
        assert "username" in params

    def test_short_value_skipped(self) -> None:
        params: dict = {}
        result = _parameterize_string("url/a", {"k": "a"}, params)
        assert "{{" not in result

    def test_no_match_unchanged(self) -> None:
        params: dict = {}
        text = "https://example.com/v1/items"
        result = _parameterize_string(text, {"email": "user@test.com"}, params)
        assert result == text


# ── _to_var_name ──────────────────────────────────────────────────────────────


class TestToVarName:
    def test_simple(self) -> None:
        assert _to_var_name("email") == "email"

    def test_camel_case(self) -> None:
        assert _to_var_name("firstName") == "first_name"

    def test_hyphen(self) -> None:
        assert _to_var_name("first-name") == "first_name"

    def test_empty_fallback(self) -> None:
        assert _to_var_name("") == "param"


# ── _parse_response_body ─────────────────────────────────────────────────────


class TestParseResponseBody:
    def test_valid_json(self) -> None:
        entry = {"response": {"content": {"text": '{"id": 1}'}}}
        assert _parse_response_body(entry) == {"id": 1}

    def test_invalid_json_returns_none(self) -> None:
        entry = {"response": {"content": {"text": "not json"}}}
        assert _parse_response_body(entry) is None

    def test_empty_text_returns_none(self) -> None:
        entry = {"response": {"content": {"text": ""}}}
        assert _parse_response_body(entry) is None

    def test_missing_content_returns_none(self) -> None:
        entry: dict = {"response": {}}
        assert _parse_response_body(entry) is None


# ── _extract_scalar_values ────────────────────────────────────────────────────


class TestExtractScalarValues:
    def test_flat_dict(self) -> None:
        result = _extract_scalar_values({"id": "abc", "name": "Test"}, "")
        paths = [r[0] for r in result]
        assert "id" in paths
        assert "name" in paths

    def test_nested_dict(self) -> None:
        result = _extract_scalar_values({"data": {"id": "nested-id"}}, "")
        assert any(v == "nested-id" for _, v in result)

    def test_list_items(self) -> None:
        result = _extract_scalar_values([{"id": "item1"}], "")
        assert any(v == "item1" for _, v in result)

    def test_max_depth_respected(self) -> None:
        deep = {"a": {"b": {"c": {"d": "deep_value"}}}}
        result = _extract_scalar_values(deep, "", max_depth=2)
        values = [v for _, v in result]
        assert "deep_value" not in values

    def test_empty_string_value_excluded(self) -> None:
        result = _extract_scalar_values({"name": ""}, "")
        assert result == []


# ── _path_to_jq & _path_to_variable_name ─────────────────────────────────────


class TestPathHelpers:
    def test_path_to_jq_adds_dot(self) -> None:
        assert _path_to_jq("data.id") == ".data.id"

    def test_path_to_jq_already_dotted(self) -> None:
        assert _path_to_jq(".id") == ".id"

    def test_path_to_variable_name_last_segment(self) -> None:
        assert _path_to_variable_name("data.customer_id") == "customer_id"

    def test_path_to_variable_name_single(self) -> None:
        assert _path_to_variable_name("id") == "id"

    def test_path_to_variable_name_array(self) -> None:
        # "[0].id" → "id"
        assert _path_to_variable_name("[0].id") == "id"


# ── _extract_api_headers ──────────────────────────────────────────────────────


class TestExtractApiHeaders:
    def test_skips_noise_headers(self) -> None:
        headers = [
            {"name": "Host", "value": "example.com"},
            {"name": "User-Agent", "value": "Mozilla/5.0"},
            {"name": "Connection", "value": "keep-alive"},
        ]
        result = _extract_api_headers(headers)
        assert result == {}

    def test_keeps_auth_header(self) -> None:
        headers = [{"name": "Authorization", "value": "Bearer tok"}]
        result = _extract_api_headers(headers)
        assert result["Authorization"] == "Bearer tok"

    def test_keeps_content_type(self) -> None:
        # Content-Type is NOT in the SKIP list in wdl_generator
        headers = [{"name": "Content-Type", "value": "application/json"}]
        result = _extract_api_headers(headers)
        assert "Content-Type" in result

    def test_skips_sec_headers(self) -> None:
        headers = [
            {"name": "sec-ch-ua", "value": '"Chrome";v="123"'},
            {"name": "Sec-Fetch-Site", "value": "same-origin"},
        ]
        result = _extract_api_headers(headers)
        assert result == {}


# ── _find_matching_narration ──────────────────────────────────────────────────


class TestFindMatchingNarration:
    def test_narration_within_10s(self) -> None:
        entry = _entry(ts="2024-01-01T12:00:05.000Z")
        narration = {"content": "Submitting the form", "timestamp": "2024-01-01T12:00:03.000Z"}
        result = _find_matching_narration(entry, [narration])
        assert result == "Submitting the form"

    def test_narration_outside_10s(self) -> None:
        entry = _entry(ts="2024-01-01T12:00:20.000Z")
        narration = {"content": "Old narration", "timestamp": "2024-01-01T12:00:05.000Z"}
        result = _find_matching_narration(entry, [narration])
        assert result == ""

    def test_no_narrations(self) -> None:
        assert _find_matching_narration(_entry(), []) == ""

    def test_closest_narration_chosen(self) -> None:
        entry = _entry(ts="2024-01-01T12:00:05.000Z")
        narrations = [
            {"content": "Far narration", "timestamp": "2024-01-01T12:00:00.000Z"},
            {"content": "Close narration", "timestamp": "2024-01-01T12:00:04.500Z"},
        ]
        result = _find_matching_narration(entry, narrations)
        assert result == "Close narration"


# ── _find_final_narration ─────────────────────────────────────────────────────


class TestFindFinalNarration:
    def test_returns_last(self) -> None:
        narrations = [
            {"content": "First step"},
            {"content": "Final result was shown"},
        ]
        assert _find_final_narration(narrations) == "Final result was shown"

    def test_empty_returns_empty(self) -> None:
        assert _find_final_narration([]) == ""

    def test_long_content_excluded(self) -> None:
        narrations = [{"content": "x" * 400}]
        assert _find_final_narration(narrations) == ""


# ── _parse_to_naive_utc ───────────────────────────────────────────────────────


class TestParseToNaiveUtc:
    def test_z_suffix(self) -> None:
        dt = _parse_to_naive_utc("2024-01-01T12:00:00Z")
        assert dt is not None
        assert dt.tzinfo is None

    def test_offset(self) -> None:
        dt = _parse_to_naive_utc("2024-01-01T13:00:00+01:00")
        assert dt is not None
        assert dt.hour == 12  # converted to UTC

    def test_aware_datetime(self) -> None:
        aware = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        result = _parse_to_naive_utc(aware)
        assert result is not None
        assert result.tzinfo is None

    def test_non_datetime_returns_none(self) -> None:
        assert _parse_to_naive_utc(42) is None  # type: ignore[arg-type]


# ── _parameterize_dict ────────────────────────────────────────────────────────


class TestParameterizeDict:
    def test_replaces_in_dict_values(self) -> None:
        params: dict = {}
        result = _parameterize_dict(
            {"email": "alice@example.com"},
            {"user_email": "alice@example.com"},
            params,
        )
        assert isinstance(result, dict)
        assert result["email"] == "{{user_email}}"

    def test_nested_dict(self) -> None:
        params: dict = {}
        result = _parameterize_dict(
            {"user": {"email": "bob@example.com"}},
            {"user_email": "bob@example.com"},
            params,
        )
        assert isinstance(result, dict)
        inner = result["user"]
        assert isinstance(inner, dict)
        assert inner["email"] == "{{user_email}}"

    def test_list_items(self) -> None:
        params: dict = {}
        result = _parameterize_dict(
            ["alice@example.com"],
            {"user_email": "alice@example.com"},
            params,
        )
        assert isinstance(result, list)
        assert result[0] == "{{user_email}}"

    def test_non_string_values_unchanged(self) -> None:
        params: dict = {}
        result = _parameterize_dict({"count": 42}, {}, params)
        assert isinstance(result, dict)
        assert result["count"] == 42
