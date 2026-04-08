"""Ephemeral credential store for autopilot runs.

Credentials live only in memory for the duration of a run.  They are
never persisted to the database, logs, or generated artifacts.
"""

from __future__ import annotations

import re
import threading

_lock = threading.Lock()
_store: dict[str, dict[str, str]] = {}

# Patterns that look like credentials in text
_SENSITIVE_PATTERNS = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|authorization|otp|mfa[_-]?code)",
    re.IGNORECASE,
)


def store_credentials(run_id: str, username: str, password: str) -> None:
    """Store credentials for a run.  Call clear_credentials when done."""
    with _lock:
        _store[run_id] = {"username": username, "password": password}


def get_credentials(run_id: str) -> dict[str, str] | None:
    """Retrieve credentials.  Returns None if not stored or already cleared."""
    with _lock:
        return _store.get(run_id)


def clear_credentials(run_id: str) -> None:
    """Remove credentials from memory."""
    with _lock:
        _store.pop(run_id, None)


def redact_text(text: str, run_id: str) -> str:
    """Replace credential values with [REDACTED] in arbitrary text."""
    creds = get_credentials(run_id)
    if not creds:
        return text
    result = text
    for value in creds.values():
        if value and len(value) >= 2:
            result = result.replace(value, "[REDACTED]")
    return result


def redact_dict(data: dict, run_id: str) -> dict:
    """Deep-redact credential values and sensitive-looking keys in a dict."""
    creds = get_credentials(run_id)
    cred_values = set()
    if creds:
        cred_values = {v for v in creds.values() if v and len(v) >= 2}

    def _walk(obj):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if _SENSITIVE_PATTERNS.search(k):
                    out[k] = "[REDACTED]"
                else:
                    out[k] = _walk(v)
            return out
        if isinstance(obj, list):
            return [_walk(item) for item in obj]
        if isinstance(obj, str):
            result = obj
            for cv in cred_values:
                result = result.replace(cv, "[REDACTED]")
            return result
        return obj

    return _walk(data)
