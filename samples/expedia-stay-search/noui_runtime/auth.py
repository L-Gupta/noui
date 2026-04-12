"""NoUI runtime auth adapter — CDP-based execution for Akamai-protected sites.

This server does NOT use the standard Tabby credential fetch + httpx pattern.
Instead, it connects directly to the Tabby browser via CDP (localhost:9222)
and executes fetch() from inside Chrome, bypassing TLS fingerprinting.

This module is kept as a stub for compatibility with the NoUI runtime
conventions. The actual auth flow is handled in each operation via CDP.
"""
from __future__ import annotations


async def resolve_auth() -> dict:
    """Stub — CDP-based operations handle auth internally.

    Returns empty headers since operations use browser-side fetch()
    with credentials: 'include' instead of Python HTTP clients.
    """
    return {}
