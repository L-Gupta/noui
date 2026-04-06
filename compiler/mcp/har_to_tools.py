"""Convert filtered HAR entries into MCP tool definitions.

Pure functions — no web framework or DB dependencies.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse, parse_qs


# ── Name generation ─────────────────────────────────────────────────────────

_METHOD_PREFIX = {
    "GET": "get",
    "POST": "create",
    "PUT": "update",
    "PATCH": "patch",
    "DELETE": "delete",
}

_API_PREFIX_RE = re.compile(
    r"^/(?:api(?:/v\d+)?|v\d+|v\d+\.\d+|rest)(/|$)", re.IGNORECASE
)

_ID_SEGMENT_RE = re.compile(r"^\d+$|^\{[^}]+\}$|^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _strip_api_prefix(path: str) -> str:
    """Strip common API prefixes like /api/v1, /v2, /rest."""
    m = _API_PREFIX_RE.match(path)
    if m:
        stripped = path[m.end() - 1:]  # keep the trailing slash context
        return stripped if stripped.startswith("/") else "/" + stripped
    return path


def _path_segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def _is_id_segment(segment: str) -> bool:
    return bool(_ID_SEGMENT_RE.match(segment))


def _generate_tool_name(method: str, path: str) -> str:
    """Generate a snake_case operation name from HTTP method + path.

    Examples:
        GET  /contacts          → list_contacts
        GET  /contacts/{id}     → get_contact
        POST /contacts          → create_contact
        PUT  /contacts/{id}     → update_contact
        DELETE /contacts/{id}   → delete_contact
        PATCH /contacts/{id}    → patch_contact
    """
    stripped = _strip_api_prefix(path)
    segments = _path_segments(stripped)

    # Filter out ID-like segments to get resource names
    resource_segments = [s for s in segments if not _is_id_segment(s)]
    has_id = any(_is_id_segment(s) for s in segments)

    if not resource_segments:
        resource_segments = ["resource"]

    # Use last non-id segment as primary resource name, singular for targeted ops
    resource = resource_segments[-1].lower()
    # Strip trailing 's' for singular (get/update/delete/patch) when targeting by ID
    if has_id and resource.endswith("s") and len(resource) > 2:
        singular = resource[:-1]
    else:
        singular = resource

    method_upper = method.upper()

    if method_upper == "GET":
        if has_id:
            prefix = "get"
            name = singular
        else:
            prefix = "list"
            name = resource
    elif method_upper == "POST":
        prefix = "create"
        name = singular
    elif method_upper == "PUT":
        prefix = "update"
        name = singular
    elif method_upper == "PATCH":
        prefix = "patch"
        name = singular
    elif method_upper == "DELETE":
        prefix = "delete"
        name = singular
    else:
        prefix = method_upper.lower()
        name = singular

    # If there are parent resources (e.g. /contacts/{id}/notes → create_contact_note)
    if len(resource_segments) > 1:
        parent = resource_segments[-2].lower()
        if parent.endswith("s") and len(parent) > 2:
            parent = parent[:-1]
        name = f"{parent}_{name}"

    # Sanitise to snake_case identifiers
    full = f"{prefix}_{name}"
    full = re.sub(r"[^a-z0-9_]", "_", full)
    full = re.sub(r"_+", "_", full).strip("_")
    return full


# ── Auth header/cookie detection ────────────────────────────────────────────

def _extract_auth_names(
    entry: dict,
    auth_info: dict,
) -> tuple[list[str], list[str]]:
    """Return (auth_header_names, auth_cookie_names) present in this entry."""
    known_auth_headers = {h.lower() for h in auth_info.get("auth_header_names", [])}
    known_auth_headers.update({"authorization", "x-auth-token", "x-api-key"})
    known_csrf_headers = {h.lower() for h in auth_info.get("csrf_header_names", [])}
    known_cookies = set(auth_info.get("set_cookie_names", []))

    req = entry.get("request", {})
    found_auth_headers: list[str] = []
    found_auth_cookies: list[str] = []

    for h in req.get("headers", []):
        hname = h.get("name", "")
        hname_lower = hname.lower()
        if hname_lower in known_auth_headers or hname_lower in known_csrf_headers:
            found_auth_headers.append(hname)

    # Parse Cookie header for known auth cookie names
    for h in req.get("headers", []):
        if h.get("name", "").lower() == "cookie":
            cookie_str = h.get("value", "")
            for cookie_pair in cookie_str.split(";"):
                cookie_pair = cookie_pair.strip()
                if "=" in cookie_pair:
                    cname = cookie_pair.split("=", 1)[0].strip()
                    if cname in known_cookies:
                        found_auth_cookies.append(cname)

    return found_auth_headers, found_auth_cookies


# ── Request body parsing ─────────────────────────────────────────────────────

def _parse_request_body(entry: dict) -> tuple[dict | None, str]:
    """Return (body_dict, content_type). body_dict is None if not JSON."""
    req = entry.get("request", {})
    post_data = req.get("postData", {}) or {}
    content_type = post_data.get("mimeType", "").lower()

    if not content_type:
        # Try to find from headers
        for h in req.get("headers", []):
            if h.get("name", "").lower() == "content-type":
                content_type = h.get("value", "").lower()
                break

    text = post_data.get("text", "")
    if not text:
        return None, content_type

    if "json" in content_type:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed, content_type
        except (json.JSONDecodeError, ValueError):
            pass

    return None, content_type


# ── Param inference ──────────────────────────────────────────────────────────

def _infer_params(
    path: str,
    query_string: list[dict],
    body: dict | None,
) -> list[dict]:
    """Infer tool parameters from path, query string, and request body."""
    params: list[dict] = []
    seen_names: set[str] = set()

    def _add(name: str, description: str, typ: str, required: bool, source: str) -> None:
        safe_name = re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "param"
        if safe_name in seen_names:
            return
        seen_names.add(safe_name)
        params.append({
            "name": safe_name,
            "description": description,
            "type": typ,
            "required": required,
            "source": source,
        })

    # Path params: explicit {id} patterns or purely numeric segments
    stripped = _strip_api_prefix(path)
    for seg in _path_segments(stripped):
        if re.match(r"^\{([^}]+)\}$", seg):
            param_name = seg[1:-1]
            _add(param_name, f"Path parameter: {param_name}", "string", True, "path")
        elif re.match(r"^\d+$", seg):
            _add("id", "Resource ID", "string", True, "path")

    # Query params
    for qs in query_string:
        name = qs.get("name", "")
        if name:
            _add(name, f"Query parameter: {name}", "string", False, "query")

    # Body params (top-level keys only)
    if body:
        for key, value in body.items():
            typ = _python_type_name(value)
            _add(key, f"Request body field: {key}", typ, True, "body")

    return params


def _python_type_name(value: object) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return "string"


# ── Non-auth/cookie header filtering ────────────────────────────────────────

_SKIP_REQUEST_HEADERS = frozenset({
    "host", "connection", "content-length", "accept-encoding",
    "accept-language", "cache-control", "pragma",
    "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "upgrade-insecure-requests", "user-agent", "referer", "origin",
    "cookie",  # cookies handled separately
})


def _clean_request_headers(entry: dict, auth_header_names: list[str]) -> list[dict]:
    """Return request headers excluding auth, cookie, and browser-internal headers."""
    auth_lower = {h.lower() for h in auth_header_names}
    cleaned = []
    for h in entry.get("request", {}).get("headers", []):
        name = h.get("name", "")
        if name.lower() in _SKIP_REQUEST_HEADERS:
            continue
        if name.lower() in auth_lower:
            continue
        cleaned.append({"name": name, "value": h.get("value", "")})
    return cleaned


# ── Main conversion ──────────────────────────────────────────────────────────

def har_entries_to_tool_defs(
    entries: list[dict],
    auth_info: dict,
    session_name: str = "",
) -> list[dict]:
    """Convert filtered HAR entries into tool definition dicts.

    Each tool def:
    {
        "name": str,
        "description": str,
        "method": str,
        "url": str,
        "path": str,
        "base_url": str,
        "request_headers": list[dict],
        "request_body": dict | None,
        "request_content_type": str,
        "response_status": int,
        "params": list[dict],
        "auth_headers": list[str],
        "auth_cookies": list[str],
    }

    Deduplicates by (method, path) — keeps the most complete example.
    """
    seen: dict[tuple[str, str], dict] = {}  # (method, normalized_path) -> tool_def

    for entry in entries:
        req = entry.get("request", {})
        resp = entry.get("response", {})

        url = req.get("url", "")
        method = req.get("method", "GET").upper()
        if not url or method == "OPTIONS":
            continue

        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path or "/"

        # Normalise path by replacing numeric IDs with {id}
        norm_path = re.sub(r"/\d+(?=/|$)", "/{id}", path)

        key = (method, norm_path)

        body, content_type = _parse_request_body(entry)
        auth_headers, auth_cookies = _extract_auth_names(entry, auth_info)
        clean_headers = _clean_request_headers(entry, auth_headers)

        # Parse query string from HAR queryString array
        qs_list = req.get("queryString", [])

        params = _infer_params(norm_path, qs_list, body)
        response_status = resp.get("status", 0)

        name = _generate_tool_name(method, norm_path)
        description = _build_description(method, norm_path, session_name)

        tool_def = {
            "name": name,
            "description": description,
            "method": method,
            "url": url,
            "path": norm_path,
            "base_url": base_url,
            "request_headers": clean_headers,
            "request_body": body,
            "request_content_type": content_type,
            "response_status": response_status,
            "params": params,
            "auth_headers": auth_headers,
            "auth_cookies": auth_cookies,
        }

        if key not in seen:
            seen[key] = tool_def
        else:
            # Keep the more complete example (more params / body present)
            existing = seen[key]
            if _completeness(tool_def) > _completeness(existing):
                seen[key] = tool_def

    # Deduplicate names (two different paths could generate the same name)
    result = list(seen.values())
    result = _deduplicate_names(result)
    return result


def _completeness(tool_def: dict) -> int:
    """Score a tool def by completeness — higher is better."""
    score = len(tool_def.get("params", []))
    if tool_def.get("request_body"):
        score += 10
    if tool_def.get("response_status", 0) in range(200, 300):
        score += 5
    return score


def _build_description(method: str, path: str, session_name: str) -> str:
    stripped = _strip_api_prefix(path)
    segments = [s for s in stripped.split("/") if s and not _is_id_segment(s)]
    resource = " ".join(segments).replace("/", " ").replace("-", " ").replace("_", " ")

    verb_map = {
        "GET": "Get" if any(_is_id_segment(s) for s in path.split("/")) else "List",
        "POST": "Create",
        "PUT": "Update",
        "PATCH": "Patch",
        "DELETE": "Delete",
    }
    verb = verb_map.get(method.upper(), method.title())
    desc = f"{verb} {resource}".strip()
    if session_name:
        desc = f"{desc} ({session_name})"
    return desc


def _deduplicate_names(tool_defs: list[dict]) -> list[dict]:
    """Ensure all tool names are unique by appending _2, _3, etc."""
    name_counts: dict[str, int] = {}
    result = []
    for td in tool_defs:
        name = td["name"]
        if name not in name_counts:
            name_counts[name] = 0
            result.append(td)
        else:
            name_counts[name] += 1
            new_td = dict(td)
            new_td["name"] = f"{name}_{name_counts[name] + 1}"
            result.append(new_td)
    return result
