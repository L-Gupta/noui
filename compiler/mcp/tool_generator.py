"""Generate Python operation module source code for a single MCP tool definition.

Pure functions — no web framework or DB dependencies.
"""

from __future__ import annotations


# ── Type mapping ─────────────────────────────────────────────────────────────

_TYPE_MAP = {
    "string": "str",
    "int": "int",
    "float": "float",
    "bool": "bool",
    "list": "list",
    "dict": "dict",
}

_DEFAULT_MAP = {
    "str": '""',
    "int": "0",
    "float": "0.0",
    "bool": "False",
    "list": "None",
    "dict": "None",
}


def _py_type(schema_type: str) -> str:
    return _TYPE_MAP.get(schema_type, "str")


def _default_value(py_type: str) -> str:
    return _DEFAULT_MAP.get(py_type, '""')


# ── Signature building ───────────────────────────────────────────────────────

def _build_signature_params(params: list[dict]) -> list[str]:
    """Return function parameter strings for the execute() signature.

    Required params come first, optional params (with defaults) after.
    """
    required = [p for p in params if p.get("required", True)]
    optional = [p for p in params if not p.get("required", True)]

    parts: list[str] = []
    for p in required:
        py_type = _py_type(p.get("type", "string"))
        if py_type in ("list", "dict"):
            parts.append(f"{p['name']}: {py_type} | None = None")
        else:
            parts.append(f"{p['name']}: {py_type}")
    for p in optional:
        py_type = _py_type(p.get("type", "string"))
        default = _default_value(py_type)
        if py_type in ("list", "dict"):
            parts.append(f"{p['name']}: {py_type} | None = None")
        else:
            parts.append(f"{p['name']}: {py_type} = {default}")

    return parts


# ── URL building ─────────────────────────────────────────────────────────────

def _build_url_expression(path: str) -> str:
    """Turn a normalised path into an f-string expression using params."""
    return f'f"{{BASE_URL}}{path}"'


# ── Recorded headers serialisation ──────────────────────────────────────────

def _format_recorded_headers(request_headers: list[dict]) -> str:
    """Render non-empty request_headers as a Python dict literal string.

    Auth headers must already have been stripped by har_to_tools before
    calling this function.  Only static headers worth preserving (Accept,
    Content-Type, etc.) are emitted here.
    """
    if not request_headers:
        return "{}"
    pairs = []
    for h in request_headers:
        name = h.get("name", "")
        value = h.get("value", "")
        if name and value:
            pairs.append(f'    {name!r}: {value!r},')
    if not pairs:
        return "{}"
    inner = "\n".join(pairs)
    return f"{{\n{inner}\n}}"


# ── Module generator ─────────────────────────────────────────────────────────

def generate_operation_module(tool: dict, auth_plan: dict | None) -> str:
    """Generate Python source code for one FastMCP operation module.

    The generated module:
    - Imports httpx and (when auth is required) the NoUI auth resolver
    - Defines an async execute(**params) function
    - Merges recorded non-auth headers with live auth headers from resolve_auth()
    - Makes the HTTP request and returns the response JSON

    Auth strategy is driven by auth_plan["strategy"]:
      - "tabby_credentials" or "static_secret_header": call resolve_auth()
      - absent/empty: plain HTTP request, recorded non-auth headers only

    Recorded non-auth headers (Accept, Content-Type, etc.) are always preserved
    and merged so they are not dropped when auth headers are added.

    Args:
        tool: Tool definition dict from har_to_tools.
        auth_plan: AuthPlan dict from auth_plan.py, or None/empty for
            unauthenticated operations.

    Returns:
        Python source as a string.
    """
    name = tool["name"]
    method = tool.get("method", "GET").upper()
    path = tool.get("path", "/")
    base_url = tool.get("base_url", "")
    description = tool.get("description", f"Execute {name}")
    params = tool.get("params", [])
    request_headers: list[dict] = tool.get("request_headers", [])

    sig_params = _build_signature_params(params)
    url_expr = _build_url_expression(path)

    body_params = [p for p in params if p.get("source") == "body"]
    query_params = [p for p in params if p.get("source") == "query"]

    # Determine whether this operation needs runtime auth
    uses_auth = bool(
        auth_plan
        and (
            auth_plan.get("required_auth", {}).get("headers")
            or auth_plan.get("required_auth", {}).get("cookies")
            or auth_plan.get("strategy") in ("tabby_credentials", "static_secret_header")
        )
    )

    has_recorded_headers = bool(_format_recorded_headers(request_headers) != "{}")

    # Build function signature string
    if sig_params:
        if len(sig_params) == 1:
            sig_str = sig_params[0]
        else:
            inner = ",\n    ".join(sig_params)
            sig_str = f"\n    {inner},\n"
    else:
        sig_str = ""

    # Determine whether to pass headers to httpx call
    needs_headers_arg = uses_auth or has_recorded_headers

    # Build HTTP method call arguments
    method_lower = method.lower()
    http_args_parts = ["url, "]
    if body_params:
        http_args_parts.append("json=body, ")
    if query_params:
        http_args_parts.append("params=qs, ")
    if needs_headers_arg:
        http_args_parts.append("headers=headers")
    else:
        # Strip trailing ", " when no headers arg follows
        if http_args_parts[-1].endswith(", "):
            http_args_parts[-1] = http_args_parts[-1][:-2]
    http_args = "".join(http_args_parts)

    # Assemble function body
    body_lines: list[str] = []

    if query_params:
        body_lines.append("    qs = {")
        for p in query_params:
            body_lines.append(f'        "{p["name"]}": {p["name"]},')
        body_lines.append("    }")
        body_lines.append('    qs = {k: v for k, v in qs.items() if v not in (None, "")}')
        body_lines.append("")

    if body_params:
        body_lines.append("    body = {")
        for p in body_params:
            body_lines.append(f'        "{p["name"]}": {p["name"]},')
        body_lines.append("    }")
        body_lines.append("")

    # Header construction: merge recorded non-auth headers with live auth headers.
    # Auth headers win on conflict; recorded non-auth headers (Accept, etc.) are kept.
    if uses_auth:
        recorded_str = _format_recorded_headers(request_headers)
        if recorded_str == "{}":
            body_lines.append("    headers = await resolve_auth()")
        else:
            body_lines.append(f"    _recorded = {recorded_str}")
            body_lines.append("    headers = {**_recorded, **await resolve_auth()}")
        body_lines.append("")
    elif has_recorded_headers:
        recorded_str = _format_recorded_headers(request_headers)
        body_lines.append(f"    headers = {recorded_str}")
        body_lines.append("")

    body_lines.append(f"    url = {url_expr}")
    body_lines.append("    async with httpx.AsyncClient() as client:")
    body_lines.append(f"        resp = await client.{method_lower}({http_args})")
    body_lines.append("        resp.raise_for_status()")
    body_lines.append("        try:")
    body_lines.append("            return resp.json()")
    body_lines.append("        except Exception:")
    body_lines.append('            return {"status": resp.status_code, "text": resp.text}')

    func_body = "\n".join(body_lines)

    auth_import = "from noui_runtime.auth import resolve_auth\n" if uses_auth else ""

    source = (
        f'"""Auto-generated operation: {name}\n'
        f"Method: {method}\n"
        f"Path: {path}\n"
        '"""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "import httpx\n"
        f"{auth_import}"
        "\n"
        f'BASE_URL = "{base_url}"\n'
        "\n"
        "\n"
        f"async def execute({sig_str}) -> dict:\n"
        f'    """{description}"""\n'
        f"{func_body}\n"
    )

    return source
