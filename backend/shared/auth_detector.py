"""Simple HAR auth pattern detector.

Pure functions — no DB dependencies.
"""

from __future__ import annotations

from urllib.parse import urlparse


def _url_domain(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return url


def detect_auth_from_har(har: dict) -> dict:
    """Extract auth signals from a HAR file.

    Returns:
        {
            "has_cookies": bool,
            "has_auth_headers": bool,
            "has_csrf": bool,
            "set_cookie_names": list[str],
            "auth_header_names": list[str],
            "csrf_header_names": list[str],
            "auth_domains": list[str],
        }
    """
    result: dict = {
        "has_cookies": False,
        "has_auth_headers": False,
        "has_csrf": False,
        "auth_domains": [],
        "set_cookie_names": [],
        "auth_header_names": [],
        "csrf_header_names": [],
    }

    if not har:
        return result

    entries = har.get("log", {}).get("entries", [])
    domains_seen: set[str] = set()
    cookie_names: list[str] = []
    auth_header_names: list[str] = []
    csrf_header_names: list[str] = []

    for entry in entries:
        response = entry.get("response", {})
        req = entry.get("request", {})
        url = req.get("url", "")
        domain = _url_domain(url)
        if domain:
            domains_seen.add(domain)

        # Check response Set-Cookie headers
        for header in response.get("headers", []):
            name = (header.get("name") or "").lower()
            if name == "set-cookie":
                result["has_cookies"] = True
                val = header.get("value", "")
                # Extract cookie name (everything before first '=')
                cookie_name = val.split("=")[0].strip()
                if cookie_name:
                    cookie_names.append(cookie_name)

        # Check request Authorization and auth-like headers
        for header in req.get("headers", []):
            hname = (header.get("name") or "").lower()
            if hname in ("authorization", "x-auth-token", "x-api-key"):
                result["has_auth_headers"] = True
                auth_header_names.append(header.get("name", ""))

        # Check for CSRF tokens in request headers
        for header in req.get("headers", []):
            hname = (header.get("name") or "").lower()
            if "csrf" in hname or "xsrf" in hname:
                result["has_csrf"] = True
                csrf_header_names.append(header.get("name", ""))

    result["auth_domains"] = list(domains_seen)
    result["set_cookie_names"] = list(dict.fromkeys(cookie_names))[:20]
    result["auth_header_names"] = list(dict.fromkeys(auth_header_names))[:10]
    result["csrf_header_names"] = list(dict.fromkeys(csrf_header_names))[:10]
    return result
