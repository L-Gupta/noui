"""Generate the noui_runtime/auth.py source code for a compiled MCP server or Skill.

Pure functions — no web framework or DB dependencies.

The same generated module is written into both output formats:

  - MCP server:  workbench/mcp_servers/<app>/<server>/noui_runtime/auth.py
  - Skill:       workbench/skills/<app>/<skill>/noui_runtime/auth.py
                 (and, after install, ~/.claude/skills/<skill>/noui_runtime/auth.py)

To work in all three locations without per-output special-casing, the generated
auth.py discovers `.env` via a portable walk-up from its own file location,
with env-var override and sensible home-dir fallbacks.
"""

from __future__ import annotations


def generate_auth_adapter(tabby_api_host: str) -> str:
    """Generate noui_runtime/auth.py source code as a string.

    The generated module provides:
      - resolve_auth()           — primary entry point, reads auth_plan.json
      - get_auth_headers(slug)   — legacy shim for backward compatibility

    Strategy is determined at runtime from auth_plan.json:
      - "tabby_credentials"    : POST /auth/agent-token + POST /credentials/request
      - "static_secret_header" : read secret from env var, construct header

    Args:
        tabby_api_host: Default Tabby API host URL baked in as a fallback
            (overridable via TABBY_API_HOST / TABBY_API_URL env var).

    Returns:
        Python source code string for noui_runtime/auth.py.
    """
    return f'''\
"""NoUI runtime auth adapter — resolves live credentials from Tabby or static secrets.

Shared across MCP-server and Skill output formats. Locates `.env` via:
  1. $NOUI_ENV_FILE — explicit override
  2. Walk up to 7 parents from this file looking for a `.env` (works both for
     in-repo layouts like workbench/mcp_servers/<app>/<server>/noui_runtime/
     and workbench/skills/<app>/<skill>/noui_runtime/, where noui/.env is
     a few parents up)
  3. ~/.config/noui/.env, then ~/.noui/.env (for skills installed outside
     the noui checkout, e.g. ~/.claude/skills/<skill>/)
  4. Skip dotenv load — rely on already-exported env vars.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv


def _find_env_file() -> Path | None:
    override = os.environ.get("NOUI_ENV_FILE")
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p

    here = Path(__file__).resolve()
    for parent in [here, *here.parents][:8]:
        candidate = parent / ".env"
        if candidate.is_file():
            return candidate

    for fallback in (Path.home() / ".config" / "noui" / ".env", Path.home() / ".noui" / ".env"):
        if fallback.is_file():
            return fallback

    return None


_env_file = _find_env_file()
if _env_file is not None:
    load_dotenv(_env_file)

TABBY_API_HOST = os.environ.get(
    "TABBY_API_URL", os.environ.get("TABBY_API_HOST", "{tabby_api_host}")
)
TABBY_CLIENT_ID = os.environ.get("TABBY_CLIENT_ID", "")
TABBY_CLIENT_SECRET = os.environ.get("TABBY_CLIENT_SECRET", "")

# auth_plan.json lives at the output root (one level up from noui_runtime/)
_AUTH_PLAN_PATH = Path(__file__).resolve().parent.parent / "auth_plan.json"


def _load_auth_plan() -> dict:
    try:
        return json.loads(_AUTH_PLAN_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {{}}
    except Exception as exc:
        raise RuntimeError(f"Failed to read auth_plan.json: {{exc}}") from exc


async def _get_agent_token() -> str:
    """Exchange client credentials for a short-lived agent JWT."""
    if not TABBY_CLIENT_ID or not TABBY_CLIENT_SECRET:
        raise RuntimeError(
            "Missing TABBY_CLIENT_ID or TABBY_CLIENT_SECRET.\\n"
            "Run `noui tabby setup` or set these vars in noui/.env "
            "(or ~/.config/noui/.env for skills installed outside the noui checkout)."
        )
    url = f"{{TABBY_API_HOST}}/auth/agent-token"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json={{
                "client_id": TABBY_CLIENT_ID,
                "client_secret": TABBY_CLIENT_SECRET,
                "grant_type": "client_credentials",
            }},
        )
        resp.raise_for_status()
        data = resp.json()
    return data.get("access_token") or data.get("token", "")


async def _tabby_credentials(profile_slug: str) -> dict:
    """Fetch live auth headers/cookies from Tabby using the 2-step credential flow.

    Uses profile_slug (not the DB UUID) for the credentials/request call.
    """
    agent_token = await _get_agent_token()
    url = f"{{TABBY_API_HOST}}/credentials/request"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json={{"profile_id": profile_slug}},
            headers={{"Authorization": f"Bearer {{agent_token}}"}},
        )
        resp.raise_for_status()
        data = resp.json()
    credentials = data.get("credentials", data)
    headers: dict[str, str] = {{}}
    for h in credentials.get("headers", []):
        if h.get("name") and h.get("value"):
            headers[h["name"]] = h["value"]
    for cookie in credentials.get("cookies", []):
        existing = headers.get("Cookie", "")
        cname = cookie.get("name", "")
        cval = cookie.get("value", "")
        if cname:
            headers["Cookie"] = f"{{existing}}; {{cname}}={{cval}}".lstrip("; ")
    return headers


def _static_secret_headers(plan: dict) -> dict:
    """Resolve static secret header(s) from environment variables."""
    headers: dict[str, str] = {{}}
    for fallback in plan.get("fallbacks", []):
        if fallback.get("type") != "static_secret_header":
            continue
        header_name = fallback["header"]
        env_var = fallback["secret_env_var"]
        value_template = fallback["value_template"]
        secret = os.environ.get(env_var, "")
        if not secret:
            raise RuntimeError(
                f"Missing required secret {{env_var}} for {{header_name}} header.\\n"
                f"Set {{env_var}} in noui/.env or export it in your shell."
            )
        placeholder = "${{" + env_var + "}}"
        headers[header_name] = value_template.replace(placeholder, secret)
    return headers


async def resolve_auth() -> dict:
    """Resolve auth headers/cookies for this server or skill based on auth_plan.json.

    Returns a dict of {{header_name: header_value}} ready to pass to httpx.
    Raises RuntimeError with a human-readable diagnostic on failure.
    """
    plan = _load_auth_plan()
    strategy = plan.get("strategy", "tabby_credentials")

    if strategy == "tabby_credentials":
        profile_slug = plan.get("profile_slug", "")
        if not profile_slug:
            raise RuntimeError(
                "auth_plan.json missing profile_slug — "
                "regenerate with `noui workflow export --as mcp --profile-slug <slug>` "
                "(or `--as skill`, `--as both`)."
            )
        creds = await _tabby_credentials(profile_slug)
        required = plan.get("required_auth", {{}}).get("headers", [])
        if required and not creds:
            raise RuntimeError(
                f"Tabby returned empty credentials for profile {{profile_slug!r}}.\\n"
                f"Required headers: {{required}}\\n"
                f"Run `noui mcp diagnose-auth <server_id>` for repair guidance "
                f"(MCP servers) or verify the Tabby profile is ACTIVE (skills)."
            )
        return creds

    elif strategy == "static_secret_header":
        return _static_secret_headers(plan)

    else:
        raise RuntimeError(
            f"Unknown auth strategy {{strategy!r}} in auth_plan.json.\\n"
            f"Regenerate this output with `noui workflow export --as mcp|skill|both`."
        )


# ---------------------------------------------------------------------------
# Legacy compatibility — servers generated before auth_plan.json was introduced
# ---------------------------------------------------------------------------


async def get_auth_headers(profile_id: str) -> dict:
    """Deprecated: prefer resolve_auth().

    Fetches credentials from Tabby using the given profile slug directly,
    bypassing auth_plan.json strategy selection.
    """
    return await _tabby_credentials(profile_id)
'''
