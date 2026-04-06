"""Generate the noui_runtime/auth.py source code for a compiled MCP server.

Pure functions — no web framework or DB dependencies.
The generated auth.py is written into the MCP server's output directory so each
server carries its own copy with the correct Tabby host baked in.
"""

from __future__ import annotations


def generate_auth_adapter(tabby_profile_id: str, tabby_api_host: str) -> str:
    """Generate noui_runtime/auth.py source code as a string.

    The generated module provides get_auth_headers(profile_id) which fetches
    live credentials from Tabby at runtime.

    Args:
        tabby_profile_id: The default profile ID for this server (informational,
            not embedded — callers pass profile_id explicitly).
        tabby_api_host: The default Tabby API host URL baked into the generated
            file as a fallback (overridable via TABBY_API_HOST env var).

    Returns:
        Python source code string for noui_runtime/auth.py.
    """
    return f'''\
"""NoUI runtime auth adapter — resolves live credentials from Tabby."""
from __future__ import annotations

import os

import httpx

TABBY_API_HOST = os.environ.get("TABBY_API_HOST", "{tabby_api_host}")


async def get_auth_headers(profile_id: str) -> dict:
    """Fetch live auth headers/cookies from Tabby for the given profile."""
    url = f"{{TABBY_API_HOST}}/runtime/credentials/{{profile_id}}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    headers: dict[str, str] = {{}}
    for h in data.get("headers", []):
        headers[h["name"]] = h["value"]
    for cookie in data.get("cookies", []):
        existing = headers.get("Cookie", "")
        cname = cookie["name"]
        cval = cookie["value"]
        headers["Cookie"] = f"{{existing}}; {{cname}}={{cval}}".lstrip("; ")
    return headers
'''
