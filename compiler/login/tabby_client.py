"""
Tabby API client for the NoUI compiler.

Provides synchronous functions to register, validate, and promote
Tabby Application + ServiceProfile records via urllib.request.

Configuration is read from backend.config.settings:
    settings.tabby_api_host   — e.g. "http://localhost:8080"
    settings.tabby_admin_token — bearer token for /admin/* endpoints
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from backend.config import settings


# ---------------------------------------------------------------------------
# Internal HTTP helper
# ---------------------------------------------------------------------------

def _tabby_http(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: int = 15,
) -> dict[str, Any] | list[Any]:
    """
    Make an HTTP request to the Tabby API.

    Raises RuntimeError on non-2xx responses.
    """
    url = settings.tabby_api_host.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else b""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {method} {path}: {body_text}") from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_alive() -> bool:
    """Return True if the Tabby API is reachable and reports healthy."""
    try:
        with urllib.request.urlopen(
            settings.tabby_api_host.rstrip("/") + "/health/live", timeout=3
        ) as resp:
            return json.loads(resp.read().decode()).get("status") == "ok"
    except Exception:
        return False


def register_application(bundle: dict, token: str) -> dict:
    """
    POST /apps with the application_draft from bundle.

    Patches http://localhost target_urls to https://localhost so Tabby
    validation does not reject local test origins.

    Returns the created application dict (includes app_id).
    Raises RuntimeError on failure.
    """
    app_draft = bundle.get("application_draft", {})

    # Rewrite http://localhost target_urls to https://localhost for Tabby
    patched_urls = [
        u.replace("http://localhost", "https://localhost", 1)
        if u.startswith("http://localhost")
        else u
        for u in (app_draft.get("target_urls") or [])
    ]
    patched_draft: dict[str, Any] = {**app_draft}
    if patched_urls:
        patched_draft["target_urls"] = patched_urls

    resp = _tabby_http("POST", "/apps", patched_draft, token=token)
    if not isinstance(resp, dict):
        raise RuntimeError(f"Unexpected response type from POST /apps: {type(resp)}")
    return resp


def register_service_profile(bundle: dict, token: str, app_id: str) -> dict:
    """
    POST /admin/profiles with the service_profile_draft from bundle,
    injecting the given app_id and a freshly computed version string.

    Returns the created profile dict (includes id as the DB primary key).
    Raises RuntimeError on failure.
    """
    profile_draft = bundle.get("service_profile_draft", {})

    t = time.localtime()
    version = f"{t.tm_year % 100}.{t.tm_mon}.{t.tm_mday}"

    profile_payload: dict[str, Any] = {
        **profile_draft,
        "app_id": app_id,
        "version": version,
    }

    resp = _tabby_http("POST", "/admin/profiles", profile_payload, token=token)
    if not isinstance(resp, dict):
        raise RuntimeError(f"Unexpected response type from POST /admin/profiles: {type(resp)}")
    return resp


def validate_profile(profile_id: str, token: str, timeout_seconds: int = 60) -> dict:
    """
    Poll GET /admin/service-profiles/{id} until the profile's version_state
    is HEALTHY (or health_result_type == PASS), or until timeout_seconds elapses.

    Returns the final profile dict.
    Raises RuntimeError if the profile reaches a terminal failure state or
    the timeout is exceeded.
    """
    deadline = time.monotonic() + timeout_seconds
    interval = 5
    last_resp: dict = {}

    while time.monotonic() < deadline:
        try:
            resp = _tabby_http("GET", f"/admin/service-profiles/{profile_id}", token=token)
            if isinstance(resp, dict):
                last_resp = resp
                state = resp.get("version_state") or resp.get("state", "")
                health = resp.get("health_result_type", "")
                if state == "HEALTHY" or health == "PASS":
                    return resp
                if state in ("FAILED", "TERMINATED") or health == "AUTH_FAIL":
                    raise RuntimeError(
                        f"Profile {profile_id} reached terminal state "
                        f"(state={state}, health={health})"
                    )
        except RuntimeError:
            raise
        except Exception:
            pass
        time.sleep(interval)

    raise RuntimeError(
        f"Profile {profile_id} did not reach HEALTHY within {timeout_seconds}s. "
        f"Last response: {last_resp}"
    )


def promote_profile(profile_db_id: str, token: str) -> dict:
    """
    POST /admin/profiles/{id}/promote.

    In the standard Tabby flow this must be called twice to move
    STAGING → CANARY → ACTIVE. This function calls it once and
    returns the updated profile dict.

    Raises RuntimeError on failure.
    """
    resp = _tabby_http(
        "POST", f"/admin/profiles/{profile_db_id}/promote", token=token
    )
    if not isinstance(resp, dict):
        raise RuntimeError(
            f"Unexpected response type from POST /admin/profiles/{profile_db_id}/promote: "
            f"{type(resp)}"
        )
    return resp
