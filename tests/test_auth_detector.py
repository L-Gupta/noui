"""Tests for backend/elicitation/auth_detector.py"""

from __future__ import annotations

from collections import Counter

from backend.elicitation.auth_detector import (
    _build_security_params,
    _determine_primary,
    _empty_details,
    _find_original_case,
    detect_auth_patterns,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _entry(headers: dict, query: list[dict] | None = None) -> dict:
    """Build a minimal HAR entry."""
    return {
        "request": {
            "headers": [{"name": k, "value": v} for k, v in headers.items()],
            "queryString": query or [],
        }
    }


# ── detect_auth_patterns ──────────────────────────────────────────────────────


class TestEmptyInput:
    def test_empty_list_returns_none(self) -> None:
        result = detect_auth_patterns([])
        assert result["primary_auth"] == "none"
        assert result["security_params"] == {}
        assert result["details"]["entries_analyzed"] == 0

    def test_entries_analyzed_count(self) -> None:
        entries = [_entry({"Authorization": "Bearer tok1"})] * 3
        result = detect_auth_patterns(entries)
        assert result["details"]["entries_analyzed"] == 3


class TestBearerDetection:
    def test_bearer_detected(self) -> None:
        entries = [_entry({"Authorization": "Bearer abc123"})]
        result = detect_auth_patterns(entries)
        assert result["primary_auth"] == "bearer"
        assert result["details"]["bearer_token_seen"] is True

    def test_bearer_case_insensitive(self) -> None:
        entries = [_entry({"authorization": "BEARER xyz"})]
        result = detect_auth_patterns(entries)
        assert result["primary_auth"] == "bearer"

    def test_bearer_security_params(self) -> None:
        entries = [_entry({"Authorization": "Bearer tok"})]
        result = detect_auth_patterns(entries)
        sp = result["security_params"]
        assert sp["type"] == "bearer"
        assert sp["token_env_var"] == "API_ACCESS_TOKEN"

    def test_bearer_increments_entries_with_auth(self) -> None:
        entries = [
            _entry({"Authorization": "Bearer t1"}),
            _entry({"Authorization": "Bearer t2"}),
        ]
        result = detect_auth_patterns(entries)
        assert result["details"]["entries_with_auth"] == 2


class TestApiKeyHeaderDetection:
    def test_x_api_key_detected(self) -> None:
        entries = [_entry({"x-api-key": "secret123"})]
        result = detect_auth_patterns(entries)
        assert result["primary_auth"] == "api_key"
        assert result["details"]["api_key_header"] is not None

    def test_original_case_preserved(self) -> None:
        entries = [_entry({"X-Api-Key": "secret"})]
        result = detect_auth_patterns(entries)
        assert result["details"]["api_key_header"] == "X-Api-Key"

    def test_apikey_header(self) -> None:
        entries = [_entry({"apikey": "mykey"})]
        result = detect_auth_patterns(entries)
        assert result["primary_auth"] == "api_key"

    def test_api_key_header_security_params(self) -> None:
        entries = [_entry({"x-api-key": "s3cr3t"})]
        result = detect_auth_patterns(entries)
        sp = result["security_params"]
        assert sp["type"] == "api_key"
        assert sp["location"] == "header"
        assert sp["key_env_var"] == "API_KEY"


class TestApiKeyQueryDetection:
    def test_api_key_in_query(self) -> None:
        entries = [_entry({}, query=[{"name": "api_key", "value": "abc"}])]
        result = detect_auth_patterns(entries)
        assert result["primary_auth"] == "api_key"
        assert result["details"]["api_key_query"] == "api_key"

    def test_token_query_param(self) -> None:
        entries = [_entry({}, query=[{"name": "token", "value": "xyz"}])]
        result = detect_auth_patterns(entries)
        assert result["primary_auth"] == "api_key"

    def test_api_key_query_security_params(self) -> None:
        # When only query param, no api_key_header is set
        entries = [_entry({}, query=[{"name": "access_token", "value": "t"}])]
        result = detect_auth_patterns(entries)
        sp = result["security_params"]
        assert sp["type"] == "api_key"
        assert sp["location"] == "query"
        assert sp["key_env_var"] == "API_KEY"


class TestBasicAuthDetection:
    def test_basic_detected(self) -> None:
        entries = [_entry({"Authorization": "Basic dXNlcjpwYXNz"})]
        result = detect_auth_patterns(entries)
        assert result["primary_auth"] == "basic"
        assert result["details"]["basic_auth_seen"] is True

    def test_basic_security_params(self) -> None:
        entries = [_entry({"Authorization": "Basic abc"})]
        result = detect_auth_patterns(entries)
        sp = result["security_params"]
        assert sp["type"] == "basic"
        assert "username_env_var" in sp
        assert "password_env_var" in sp


class TestCookieAuthDetection:
    def test_session_cookie_detected(self) -> None:
        entries = [_entry({"Cookie": "session=abc123def456"})]
        result = detect_auth_patterns(entries)
        assert result["primary_auth"] == "cookie"
        assert result["details"]["cookie_auth_seen"] is True

    def test_auth_cookie_keyword(self) -> None:
        entries = [_entry({"Cookie": "auth=mytoken"})]
        result = detect_auth_patterns(entries)
        assert result["primary_auth"] == "cookie"

    def test_jwt_cookie(self) -> None:
        entries = [_entry({"Cookie": "jwt=eyJhbGci..."})]
        result = detect_auth_patterns(entries)
        assert result["primary_auth"] == "cookie"

    def test_cookie_with_authorization_uses_bearer(self) -> None:
        # Bearer takes priority over cookie
        entries = [
            _entry(
                {
                    "Authorization": "Bearer tok",
                    "Cookie": "session=abc",
                }
            )
        ]
        result = detect_auth_patterns(entries)
        assert result["primary_auth"] == "bearer"

    def test_plain_cookie_not_auth(self) -> None:
        # Cookie without session/auth keywords shouldn't trigger cookie auth
        entries = [_entry({"Cookie": "theme=dark; lang=en"})]
        result = detect_auth_patterns(entries)
        assert result["primary_auth"] == "none"

    def test_cookie_security_params(self) -> None:
        entries = [_entry({"Cookie": "sid=abc"})]
        result = detect_auth_patterns(entries)
        sp = result["security_params"]
        assert sp["type"] == "cookie"
        assert "note" in sp


class TestNoAuth:
    def test_no_auth_headers(self) -> None:
        entries = [_entry({"Content-Type": "application/json", "Accept": "*/*"})]
        result = detect_auth_patterns(entries)
        assert result["primary_auth"] == "none"
        assert result["security_params"] == {}

    def test_mixed_no_auth_and_auth_entries(self) -> None:
        entries = [
            _entry({"Content-Type": "application/json"}),
            _entry({"Authorization": "Bearer tok"}),
        ]
        result = detect_auth_patterns(entries)
        assert result["primary_auth"] == "bearer"


# ── _determine_primary ────────────────────────────────────────────────────────


class TestDeterminePrimary:
    def test_empty_counts_returns_none(self) -> None:
        assert _determine_primary(Counter(), {}) == "none"

    def test_bearer_wins_over_api_key(self) -> None:
        counts: Counter = Counter({"bearer": 1, "api_key": 5})
        assert _determine_primary(counts, {}) == "bearer"

    def test_api_key_wins_over_basic(self) -> None:
        counts: Counter = Counter({"api_key": 1, "basic": 2})
        assert _determine_primary(counts, {}) == "api_key"

    def test_api_key_query_returns_api_key(self) -> None:
        counts: Counter = Counter({"api_key_query": 3})
        assert _determine_primary(counts, {}) == "api_key"

    def test_basic_auth(self) -> None:
        counts: Counter = Counter({"basic": 2})
        assert _determine_primary(counts, {}) == "basic"

    def test_cookie_auth(self) -> None:
        counts: Counter = Counter({"cookie": 1})
        assert _determine_primary(counts, {}) == "cookie"


# ── _find_original_case ───────────────────────────────────────────────────────


class TestFindOriginalCase:
    def test_finds_mixed_case_header(self) -> None:
        headers = [{"name": "X-Api-Key", "value": "v"}]
        assert _find_original_case(headers, "x-api-key") == "X-Api-Key"

    def test_not_found_returns_lower_name(self) -> None:
        headers: list[dict] = []
        assert _find_original_case(headers, "x-api-key") == "x-api-key"

    def test_exact_lowercase_match(self) -> None:
        headers = [{"name": "apikey", "value": "v"}]
        assert _find_original_case(headers, "apikey") == "apikey"


# ── _build_security_params ────────────────────────────────────────────────────


class TestBuildSecurityParams:
    def test_bearer(self) -> None:
        result = _build_security_params("bearer", _empty_details())
        assert result == {"type": "bearer", "token_env_var": "API_ACCESS_TOKEN"}

    def test_api_key_header(self) -> None:
        details = _empty_details()
        details["api_key_header"] = "X-Api-Key"
        result = _build_security_params("api_key", details)
        assert result["location"] == "header"
        assert result["name"] == "X-Api-Key"

    def test_api_key_query(self) -> None:
        details = _empty_details()
        details["api_key_query"] = "api_key"
        result = _build_security_params("api_key", details)
        assert result["location"] == "query"
        assert result["name"] == "api_key"

    def test_none_returns_empty(self) -> None:
        assert _build_security_params("none", _empty_details()) == {}
