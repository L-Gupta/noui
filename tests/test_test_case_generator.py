"""Tests for backend/elicitation/test_case_generator.py"""

from __future__ import annotations

import json

from backend.elicitation.test_case_generator import (
    _derive_expected_output,
    _derive_prompt,
    _derive_validation,
    _derive_workflow_params,
    _slugify,
    _to_var,
    generate_test_cases,
)

# ── generate_test_cases ──────────────────────────────────────────────────────


class TestGenerateTestCases:
    def test_always_returns_happy_path(self) -> None:
        cases = generate_test_cases("My Process")
        assert len(cases) >= 1
        assert cases[0]["test_case_name"] == "happy_path_my_process"

    def test_happy_path_has_required_keys(self) -> None:
        cases = generate_test_cases("P")
        tc = cases[0]
        assert "test_case_name" in tc
        assert "prompt" in tc
        assert "workflow_params" in tc
        assert "expected_output" in tc
        assert "validation" in tc

    def test_missing_params_stub_added_when_form_inputs(self) -> None:
        wdl_params = {
            "email": {"example": "user@example.com", "source": "form_input"},
        }
        cases = generate_test_cases("P", wdl_params=wdl_params)
        names = [c["test_case_name"] for c in cases]
        assert "missing_params_p" in names

    def test_no_missing_params_stub_without_form_inputs(self) -> None:
        wdl_params = {
            "base_url": {"example": "https://api.example.com", "source": "har_analysis"},
        }
        cases = generate_test_cases("P", wdl_params=wdl_params)
        assert len(cases) == 1

    def test_missing_params_stub_has_empty_params(self) -> None:
        wdl_params = {"field": {"source": "form_input"}}
        cases = generate_test_cases("P", wdl_params=wdl_params)
        stub = next(c for c in cases if "missing" in c["test_case_name"])
        assert stub["workflow_params"] == {}
        assert stub["expected_output"] == "error"

    def test_prompt_from_narration(self) -> None:
        narrations = [{"content": "Click the create button to submit the form"}]
        cases = generate_test_cases("P", narrations=narrations)
        assert cases[0]["prompt"] == "Click the create button to submit the form"

    def test_prompt_fallback(self) -> None:
        cases = generate_test_cases("Invoice Workflow")
        assert "Invoice Workflow" in cases[0]["prompt"]

    def test_workflow_params_from_clicks(self) -> None:
        clicks = [
            {"event_type": "input", "field_name": "email", "value": "alice@example.com"},
        ]
        cases = generate_test_cases("P", click_events=clicks)
        assert "email" in cases[0]["workflow_params"]
        assert cases[0]["workflow_params"]["email"] == "alice@example.com"


# ── _derive_prompt ───────────────────────────────────────────────────────────


class TestDerivePrompt:
    def test_from_first_narration(self) -> None:
        narrations = [{"content": "Navigate to the dashboard and click submit"}]
        assert _derive_prompt("P", narrations) == "Navigate to the dashboard and click submit"

    def test_short_narration_ignored(self) -> None:
        narrations = [{"content": "Hi"}]
        result = _derive_prompt("My Process", narrations)
        assert "My Process" in result

    def test_empty_narrations_fallback(self) -> None:
        result = _derive_prompt("Create Invoice", [])
        assert "Create Invoice" in result


# ── _derive_workflow_params ──────────────────────────────────────────────────


class TestDeriveWorkflowParams:
    def test_extracts_input_events(self) -> None:
        clicks = [
            {"event_type": "input", "field_name": "username", "value": "alice"},
        ]
        params = _derive_workflow_params(clicks, {})
        assert "username" in params
        assert params["username"] == "alice"

    def test_extracts_change_events(self) -> None:
        clicks = [
            {"event_type": "change", "field_name": "country", "value": "US"},
        ]
        params = _derive_workflow_params(clicks, {})
        assert "country" in params

    def test_non_input_events_skipped(self) -> None:
        clicks = [{"event_type": "click", "field_name": "submit", "value": "Submit"}]
        params = _derive_workflow_params(clicks, {})
        assert params == {}

    def test_empty_value_skipped(self) -> None:
        clicks = [{"event_type": "input", "field_name": "email", "value": ""}]
        params = _derive_workflow_params(clicks, {})
        assert params == {}

    def test_deduplication(self) -> None:
        # Same field_name appears twice — only first value kept
        clicks = [
            {"event_type": "input", "field_name": "email", "value": "a@b.com"},
            {"event_type": "input", "field_name": "email", "value": "c@d.com"},
        ]
        params = _derive_workflow_params(clicks, {})
        assert params["email"] == "a@b.com"

    def test_uses_element_id_fallback(self) -> None:
        clicks = [
            {"event_type": "input", "field_name": "", "element_id": "input_name", "value": "Bob"}
        ]
        params = _derive_workflow_params(clicks, {})
        assert "input_name" in params


# ── _derive_expected_output ──────────────────────────────────────────────────


class TestDeriveExpectedOutput:
    def test_no_entries_fallback(self) -> None:
        result = _derive_expected_output([])
        assert "successfully" in result.lower()

    def test_status_code_fallback(self) -> None:
        entry = {"response": {"status": 204, "content": {"text": ""}}}
        result = _derive_expected_output([entry])
        assert "204" in result

    def test_json_message_field(self) -> None:
        body = json.dumps({"message": "Invoice created"})
        entry = {"response": {"status": 201, "content": {"text": body}}}
        result = _derive_expected_output([entry])
        assert result == "Invoice created"

    def test_json_status_field(self) -> None:
        body = json.dumps({"status": "OK"})
        entry = {"response": {"status": 200, "content": {"text": body}}}
        assert _derive_expected_output([entry]) == "OK"

    def test_json_list_response(self) -> None:
        body = json.dumps([1, 2, 3])
        entry = {"response": {"status": 200, "content": {"text": body}}}
        result = _derive_expected_output([entry])
        assert "3" in result

    def test_json_fallback_first_keys(self) -> None:
        body = json.dumps({"id": "abc", "name": "Test", "status": "active"})
        entry = {"response": {"status": 200, "content": {"text": body}}}
        result = _derive_expected_output([entry])
        # status is a priority key, should return "active"
        assert "active" in result

    def test_short_text_response(self) -> None:
        entry = {"response": {"status": 200, "content": {"text": "Done"}}}
        result = _derive_expected_output([entry])
        assert "Done" in result

    def test_uses_last_entry(self) -> None:
        entries = [
            {"response": {"status": 200, "content": {"text": json.dumps({"message": "first"})}}},
            {"response": {"status": 200, "content": {"text": json.dumps({"message": "last"})}}},
        ]
        result = _derive_expected_output(entries)
        assert result == "last"


# ── _derive_validation ───────────────────────────────────────────────────────


class TestDeriveValidation:
    def test_empty_output(self) -> None:
        v = _derive_validation("")
        assert v["type"] == "contains"

    def test_short_output_below_5_chars(self) -> None:
        # < 5 chars → empty value (too short to be meaningful)
        v = _derive_validation("OK")
        assert v["type"] == "contains"
        assert v["value"] == ""

    def test_medium_output_uses_full_value(self) -> None:
        msg = "Invoice created successfully"
        v = _derive_validation(msg)
        assert v["value"] == msg

    def test_long_output_uses_first_sentence(self) -> None:
        msg = "A" * 110 + ". " + "B" * 50
        v = _derive_validation(msg)
        assert len(v["value"]) <= 110

    def test_long_output_with_sentence_uses_first_sentence(self) -> None:
        # "A"*200 has no period, so first_sentence = all 200 chars which is > 10 → returned as-is
        # To hit the [:60] branch we need a very short first sentence
        msg = "Hi. " + "B" * 200
        v = _derive_validation(msg)
        # "Hi" is 2 chars ≤ 10, so falls through to [:60]
        assert len(v["value"]) <= 60


# ── _slugify ─────────────────────────────────────────────────────────────────


class TestSlugify:
    def test_basic(self) -> None:
        assert _slugify("My Process") == "my_process"

    def test_special_chars(self) -> None:
        assert _slugify("Hello World!") == "hello_world"

    def test_numbers_preserved(self) -> None:
        assert _slugify("Step 2") == "step_2"

    def test_empty_string(self) -> None:
        assert _slugify("") == "unnamed"

    def test_leading_trailing_underscores_stripped(self) -> None:
        assert not _slugify("  My  ").startswith("_")


# ── _to_var ───────────────────────────────────────────────────────────────────


class TestToVar:
    def test_simple_name(self) -> None:
        assert _to_var("email") == "email"

    def test_hyphenated(self) -> None:
        assert _to_var("first-name") == "first_name"

    def test_camel_case(self) -> None:
        assert _to_var("firstName") == "first_name"

    def test_spaces(self) -> None:
        assert _to_var("field name") == "field_name"

    def test_dots(self) -> None:
        assert _to_var("user.email") == "user_email"

    def test_special_chars_removed(self) -> None:
        assert _to_var("field@name!") == "fieldname"

    def test_empty_fallback(self) -> None:
        assert _to_var("") == "param"
