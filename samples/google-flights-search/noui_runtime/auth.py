"""NoUI runtime auth adapter — resolves live credentials from Tabby."""
from __future__ import annotations

import os

import httpx

TABBY_API_HOST = os.environ.get("TABBY_API_HOST", "http://localhost:8080")


async def get_auth_headers(profile_id: str) -> dict:
    """Fetch live auth headers/cookies from Tabby for the given profile."""
    url = f"{TABBY_API_HOST}/runtime/credentials/{profile_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    headers: dict[str, str] = {}
    for h in data.get("headers", []):
        headers[h["name"]] = h["value"]
    for cookie in data.get("cookies", []):
        existing = headers.get("Cookie", "")
        cname = cookie["name"]
        cval = cookie["value"]
        headers["Cookie"] = f"{existing}; {cname}={cval}".lstrip("; ")
    return headers
