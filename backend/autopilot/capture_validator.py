"""Validate captured HAR before MCP generation.

Returns a list of warnings and a pass/fail verdict so the controller can
decide whether to proceed with export or abort with an explanation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


@dataclass
class ValidationResult:
    passed: bool = True
    warnings: list[str] = field(default_factory=list)
    api_call_count: int = 0
    domains: list[str] = field(default_factory=list)

    def fail(self, reason: str) -> None:
        self.passed = False
        self.warnings.append(f"FAIL: {reason}")

    def warn(self, reason: str) -> None:
        self.warnings.append(f"WARN: {reason}")


# Extensions that are almost certainly static assets, not API calls.
_STATIC_EXTENSIONS = {
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".map", ".webp", ".avif",
}

# Keys whose values look like credentials.
_CRED_KEYS = re.compile(
    r"(password|passwd|secret|api[_-]?key|token|authorization|otp|mfa)",
    re.IGNORECASE,
)

# Content types that look like JSON API responses.
_API_CONTENT_TYPES = {"application/json", "application/graphql+json"}


def _is_api_call(entry: dict) -> bool:
    """Heuristic: is this HAR entry likely a real API call?"""
    req = entry.get("request", {})
    resp = entry.get("response", {})

    url = req.get("url", "")
    method = req.get("method", "GET").upper()
    path = urlparse(url).path.lower()

    # Skip static assets
    for ext in _STATIC_EXTENSIONS:
        if path.endswith(ext):
            return False

    # Mutation methods are almost always API calls
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        return True

    # GET with JSON response is likely an API call
    resp_content_type = ""
    for header in resp.get("headers", []):
        if header.get("name", "").lower() == "content-type":
            resp_content_type = header.get("value", "").lower()
            break

    for ct in _API_CONTENT_TYPES:
        if ct in resp_content_type:
            return True

    # GET with /api/ in path
    if "/api/" in path or path.startswith("/graphql"):
        return True

    return False


def _check_credential_leak(entry: dict) -> str | None:
    """Return a warning string if the entry body contains credential-like keys."""
    req = entry.get("request", {})
    post_data = req.get("postData", {})
    text = post_data.get("text", "")

    if not text:
        return None

    # Try parsing as JSON
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        body = None

    if isinstance(body, dict):
        for key in body:
            if _CRED_KEYS.search(key):
                return f"Request body contains credential-like key '{key}' in {req.get('url', '?')}"

    # Also check raw text for password= form params
    if "password=" in text.lower() or "passwd=" in text.lower():
        return f"Request body contains credential-like form param in {req.get('url', '?')}"

    return None


def validate_har(
    har_path: str | Path,
    expected_domain: str = "",
) -> ValidationResult:
    """Validate a HAR file for autopilot recording quality.

    Args:
        har_path: Path to the HAR JSON file.
        expected_domain: The hostname the workflow should target.

    Returns:
        ValidationResult with pass/fail and warnings.
    """
    result = ValidationResult()
    har_path = Path(har_path)

    # 1. File must exist
    if not har_path.exists():
        result.fail("HAR file does not exist")
        return result

    # 2. Must be valid JSON
    try:
        with open(har_path) as f:
            har = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        result.fail(f"HAR file is not valid JSON: {exc}")
        return result

    entries = har.get("log", {}).get("entries", [])
    if not entries:
        result.fail("HAR contains no entries")
        return result

    # 3. Count API calls
    api_entries = [e for e in entries if _is_api_call(e)]
    result.api_call_count = len(api_entries)

    if result.api_call_count == 0:
        result.fail(
            "HAR contains no likely API calls after filtering static assets. "
            "The capture may have only recorded HTML page loads."
        )
        return result

    # 4. Collect domains
    seen_domains: set[str] = set()
    for entry in entries:
        url = entry.get("request", {}).get("url", "")
        host = urlparse(url).hostname
        if host:
            seen_domains.add(host)
    result.domains = sorted(seen_domains)

    # 5. Check domain match
    if expected_domain:
        expected_host = urlparse(
            expected_domain if "://" in expected_domain else f"https://{expected_domain}"
        ).hostname
        matching = [d for d in seen_domains if expected_host and expected_host in d]
        unrelated = seen_domains - set(matching)
        if not matching:
            result.warn(
                f"No captured requests match the expected domain '{expected_host}'. "
                f"Captured domains: {', '.join(sorted(seen_domains))}"
            )
        if unrelated:
            result.warn(
                f"Capture includes unrelated domains: {', '.join(sorted(unrelated))}"
            )

    # 6. Check for credential leaks
    for entry in api_entries:
        leak = _check_credential_leak(entry)
        if leak:
            result.warn(f"Potential credential leak: {leak}")

    # 7. Warn on very low API call count
    if result.api_call_count < 2:
        result.warn(
            f"Only {result.api_call_count} API call(s) found. "
            "The generated MCP may have very few tools."
        )

    # 8. Check for mutation calls (POST/PUT/PATCH/DELETE)
    mutation_count = sum(
        1 for e in api_entries
        if e.get("request", {}).get("method", "GET").upper()
        in ("POST", "PUT", "PATCH", "DELETE")
    )
    if mutation_count == 0:
        result.warn(
            "No mutation (POST/PUT/PATCH/DELETE) API calls captured. "
            "The workflow may not have performed the intended action."
        )

    return result
