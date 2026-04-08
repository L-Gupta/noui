"""Tests for backend/elicitation/requirements_generator.py"""

from __future__ import annotations

from datetime import datetime

from backend.elicitation.requirements_generator import (
    _extract_workflow_steps,
    _format_timestamp,
    _looks_like_intent,
    generate_requirements_md,
)

# ── generate_requirements_md ─────────────────────────────────────────────────


class TestGenerateRequirementsMd:
    def test_header_contains_process_name(self) -> None:
        md = generate_requirements_md("My Process", "A description")
        assert "# Requirements: My Process" in md

    def test_overview_from_description(self) -> None:
        md = generate_requirements_md("P", "This is the description")
        assert "This is the description" in md

    def test_overview_from_narration_when_no_description(self) -> None:
        narrations = [{"content": "User clicks submit button", "timestamp": None}]
        md = generate_requirements_md("P", "", narrations=narrations)
        assert "User clicks submit button" in md

    def test_no_description_fallback(self) -> None:
        md = generate_requirements_md("P", "")
        assert "No description available" in md

    def test_narrations_section(self) -> None:
        narrations = [
            {
                "content": "First step narration",
                "timestamp": "2024-01-01T10:00:00Z",
                "url": "https://example.com",
            },
        ]
        md = generate_requirements_md("P", "desc", narrations=narrations)
        assert "## User Narrations" in md
        assert "First step narration" in md
        assert "https://example.com" in md

    def test_narration_without_url(self) -> None:
        narrations = [{"content": "Click something", "timestamp": None, "url": ""}]
        md = generate_requirements_md("P", "desc", narrations=narrations)
        assert "Click something" in md
        # No URL line appended
        assert "*(on" not in md

    def test_captured_parameters_section(self) -> None:
        click_events = [
            {
                "event_type": "input",
                "field_name": "email",
                "value": "test@example.com",
                "input_type": "email",
            },
            {"event_type": "click", "field_name": "submit", "value": ""},  # click events excluded
        ]
        md = generate_requirements_md("P", "desc", click_events=click_events)
        assert "## Captured Parameters" in md
        assert "email" in md
        assert "test@example.com" in md

    def test_captured_parameters_deduplication(self) -> None:
        click_events = [
            {
                "event_type": "input",
                "field_name": "email",
                "value": "a@b.com",
                "input_type": "email",
            },
            {
                "event_type": "input",
                "field_name": "email",
                "value": "c@d.com",
                "input_type": "email",
            },
        ]
        md = generate_requirements_md("P", "desc", click_events=click_events)
        # Should appear only once
        assert md.count("email") == md.count("email")  # basic sanity
        lines_with_email = [ln for ln in md.splitlines() if "| email |" in ln]
        assert len(lines_with_email) == 1

    def test_click_events_without_field_name_excluded(self) -> None:
        click_events = [
            {"event_type": "input", "field_name": "", "value": "v", "input_type": "text"}
        ]
        md = generate_requirements_md("P", "desc", click_events=click_events)
        assert "## Captured Parameters" not in md

    def test_questions_answered_section(self) -> None:
        questions = [
            {"content": "What is the API?", "answer": "REST", "status": "answered"},
        ]
        md = generate_requirements_md("P", "desc", questions=questions)
        assert "## Questions" in md
        assert "### Answered" in md
        assert "What is the API?" in md
        assert "REST" in md

    def test_questions_open_section(self) -> None:
        questions = [
            {"content": "Pending question?", "answer": "", "status": "open"},
        ]
        md = generate_requirements_md("P", "desc", questions=questions)
        assert "### Open" in md
        assert "Pending question?" in md

    def test_workflow_steps_from_timeline(self) -> None:
        timeline = [
            {"event_type": "url_change", "summary": "Navigated to /dashboard"},
            {"event_type": "click", "summary": "Clicked Submit"},
        ]
        md = generate_requirements_md("P", "desc", timeline_events=timeline)
        assert "## Workflow Steps" in md
        assert "Navigate: Navigated to /dashboard" in md
        assert "Click: Clicked Submit" in md

    def test_notes_from_conversation(self) -> None:
        messages = [
            {"role": "human", "content": "I want to create a new invoice", "timestamp": None},
            {"role": "assistant", "content": "Sure!", "timestamp": None},  # assistant excluded
        ]
        md = generate_requirements_md("P", "desc", messages=messages)
        assert "## Notes from Conversation" in md
        assert "create a new invoice" in md
        assert "Sure!" not in md

    def test_notes_truncated_at_200_chars(self) -> None:
        long_msg = "I need to " + "x" * 300
        messages = [{"role": "human", "content": long_msg, "timestamp": None}]
        md = generate_requirements_md("P", "desc", messages=messages)
        assert "..." in md

    def test_empty_inputs(self) -> None:
        md = generate_requirements_md("Empty Process", "")
        assert "# Requirements: Empty Process" in md
        assert "## User Narrations" not in md
        assert "## Captured Parameters" not in md
        assert "## Questions" not in md


# ── _extract_workflow_steps ───────────────────────────────────────────────────


class TestExtractWorkflowSteps:
    def test_url_change(self) -> None:
        events = [{"event_type": "url_change", "summary": "Loaded /home"}]
        steps = _extract_workflow_steps(events)
        assert steps == ["Navigate: Loaded /home"]

    def test_click(self) -> None:
        events = [{"event_type": "click", "summary": "Pressed OK"}]
        assert _extract_workflow_steps(events) == ["Click: Pressed OK"]

    def test_input(self) -> None:
        events = [{"event_type": "input", "summary": "Typed email"}]
        assert _extract_workflow_steps(events) == ["Input: Typed email"]

    def test_change(self) -> None:
        events = [{"event_type": "change", "summary": "Changed value"}]
        assert _extract_workflow_steps(events) == ["Input: Changed value"]

    def test_narration(self) -> None:
        events = [{"event_type": "narration", "summary": "Explained the step"}]
        assert _extract_workflow_steps(events) == ["Narration: Explained the step"]

    def test_network_request(self) -> None:
        events = [{"event_type": "network_request", "summary": "POST /api/data"}]
        assert _extract_workflow_steps(events) == ["API call: POST /api/data"]

    def test_unknown_event_type_skipped(self) -> None:
        events = [{"event_type": "screenshot", "summary": "Captured"}]
        assert _extract_workflow_steps(events) == []

    def test_empty_summary_skipped(self) -> None:
        events = [{"event_type": "click", "summary": ""}]
        assert _extract_workflow_steps(events) == []

    def test_empty_list(self) -> None:
        assert _extract_workflow_steps([]) == []

    def test_multiple_events(self) -> None:
        events = [
            {"event_type": "url_change", "summary": "Loaded page"},
            {"event_type": "click", "summary": "Clicked button"},
        ]
        steps = _extract_workflow_steps(events)
        assert len(steps) == 2


# ── _format_timestamp ─────────────────────────────────────────────────────────


class TestFormatTimestamp:
    def test_none_returns_question_mark(self) -> None:
        assert _format_timestamp(None) == "?"

    def test_empty_string_returns_question_mark(self) -> None:
        assert _format_timestamp("") == "?"

    def test_datetime_object(self) -> None:
        dt = datetime(2024, 6, 15, 10, 30, 45)
        assert _format_timestamp(dt) == "10:30:45"

    def test_iso_string_with_z(self) -> None:
        result = _format_timestamp("2024-06-15T10:30:45Z")
        assert result == "10:30:45"

    def test_iso_string_with_offset(self) -> None:
        result = _format_timestamp("2024-06-15T11:30:45+01:00")
        assert result == "11:30:45"

    def test_invalid_string_fallback(self) -> None:
        result = _format_timestamp("not-a-date")
        assert result == "not-a-da"  # str(ts)[:8]

    def test_numeric_fallback(self) -> None:
        result = _format_timestamp(12345678)
        assert result == "12345678"


# ── _looks_like_intent ────────────────────────────────────────────────────────


class TestLooksLikeIntent:
    def test_empty_string(self) -> None:
        assert _looks_like_intent("") is False

    def test_short_string(self) -> None:
        assert _looks_like_intent("hi") is False

    def test_greetings_excluded(self) -> None:
        for greeting in ("hi", "hello", "thanks", "ok", "yes", "no", "sure"):
            assert _looks_like_intent(greeting) is False, greeting

    def test_intent_with_want(self) -> None:
        assert _looks_like_intent("I want to create a new workflow") is True

    def test_intent_with_need(self) -> None:
        assert _looks_like_intent("I need to automate this process") is True

    def test_intent_with_api(self) -> None:
        assert _looks_like_intent("This calls the API endpoint") is True

    def test_intent_with_create(self) -> None:
        assert _looks_like_intent("Please create a report for me") is True

    def test_no_intent_words(self) -> None:
        assert _looks_like_intent("The browser just opened the page here") is False

    def test_case_insensitive(self) -> None:
        assert _looks_like_intent("I WANT to update the record") is True
