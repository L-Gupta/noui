"""Thin wrapper around the NoUI backend browser-command bridge.

Calls POST http://localhost:8002/browser-commands/execute which forwards
each command to the Chrome extension connected to the NoUI backend.

Prerequisites:
  - NoUI backend running: python cli/main.py start  (or uvicorn backend.main:app)
  - Chrome extension loaded and showing a green dot at localhost:8002

Available command_types mirror what the extension supports:
  navigate, click_element, click_by_text, wait_for_selector, wait_for_url,
  get_page_info, eval_js, query_elements, cdp_click, cdp_type, get_page_summary

All public functions raise RuntimeError on command failure so callers get a
clear traceback rather than a silent empty result.
"""

from __future__ import annotations

import asyncio

import httpx

BACKEND_URL = "http://localhost:8002"
_EXECUTE_ENDPOINT = f"{BACKEND_URL}/browser-commands/execute"
_DEFAULT_TIMEOUT = 35.0  # seconds — the backend itself times out commands at 30 s

# Error substrings that indicate a transient frame-removal after navigation.
# After a full-page navigation the old frame is destroyed; the extension may
# report this on the very next command.  We retry once after a short pause.
_FRAME_REMOVED_HINTS = ("frame with id", "was removed", "frame removed")


async def _execute(command_type: str, params: dict, *, _retry: bool = True) -> dict:
    """Post a single command to the backend bridge and return the result dict."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _EXECUTE_ENDPOINT,
            json={"command_type": command_type, "params": params},
            timeout=_DEFAULT_TIMEOUT,
        )
    if resp.status_code == 504:
        raise RuntimeError(
            f"Browser command '{command_type}' timed out — "
            "make sure the Chrome extension is open and connected."
        )
    resp.raise_for_status()
    envelope = resp.json()

    # The backend wraps results as {"success": bool, "data": {...}, "error": ...}.
    # Unwrap so callers receive the payload dict directly.
    err = envelope.get("error") or ""
    if err:
        # Retry once for transient frame-removal errors caused by navigation.
        if _retry and any(h in err.lower() for h in _FRAME_REMOVED_HINTS):
            await asyncio.sleep(1.5)
            return await _execute(command_type, params, _retry=False)
        raise RuntimeError(
            f"Browser command '{command_type}' failed: {err}"
        )
    data = envelope.get("data")
    if isinstance(data, dict):
        return data
    # Some commands return the payload at the top level (no data wrapper).
    return envelope


async def navigate(url: str) -> dict:
    """Navigate the active Chrome tab to *url*.

    Returns ``{"success": true, "url": url}`` when the navigation was
    accepted by the extension.  Does not wait for page load — follow
    with ``wait_for_selector`` or ``wait_for_url`` as needed.
    """
    return await _execute("navigate", {"url": url})


async def wait_for_selector(selector: str, *, timeout_ms: int = 12000) -> dict:
    """Block until *selector* appears in the DOM or timeout expires.

    Returns ``{"found": bool, "waited": <ms>}``.
    """
    return await _execute(
        "wait_for_selector", {"selector": selector, "timeout": timeout_ms}
    )


async def wait_for_url(url_substring: str, *, timeout_ms: int = 12000) -> dict:
    """Block until the page URL contains *url_substring* or timeout expires."""
    return await _execute(
        "wait_for_url", {"url_substring": url_substring, "timeout": timeout_ms}
    )


async def click_by_text(text: str, *, exact: bool = False) -> dict:
    """Click the first visible interactive element whose label contains *text*.

    Returns ``{"clicked": bool, ...}``.
    """
    return await _execute("click_by_text", {"text": text, "exact": exact})


async def click_element(selector: str) -> dict:
    """Click the element identified by CSS *selector*.

    Returns ``{"clicked": bool, ...}``.
    """
    return await _execute("click_element", {"selector": selector})


async def cdp_click(selector: str) -> dict:
    """Dispatch OS-level mouse events via CDP (works on React/Vue components).

    Use when ``click_element`` fails silently on custom components.
    """
    return await _execute("cdp_click", {"selector": selector})


async def get_page_info() -> dict:
    """Return ``{"url": str, "title": str}`` for the active tab."""
    return await _execute("get_page_info", {})


async def get_page_summary() -> dict:
    """Return a summary of visible interactive elements on the page."""
    return await _execute("get_page_summary", {})


async def query_elements(
    selector: str, *, include_text: bool = True, max_results: int = 20
) -> dict:
    """Query elements matching *selector* and return their attributes.

    Returns ``{"count": N, "elements": [...]}``.
    """
    return await _execute(
        "query_elements",
        {"selector": selector, "includeText": include_text, "maxResults": max_results},
    )


async def eval_js(code: str) -> dict:
    """Evaluate a JavaScript expression in the active tab.

    Returns ``{"success": bool, "result": any}``.  Note: blocked by CSP
    on some pages — prefer other commands when possible.
    """
    return await _execute("eval_js", {"code": code})
