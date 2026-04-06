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


# ── Module generator ─────────────────────────────────────────────────────────

def generate_operation_module(tool: dict, tabby_profile_id: str) -> str:
    """Generate Python source code for one FastMCP operation module.

    The generated module:
    - Imports httpx and the NoUI auth adapter
    - Defines an async execute(**params) function
    - Fetches auth credentials at runtime via auth adapter
    - Makes the HTTP request
    - Returns the response JSON

    Returns Python source as a string.
    """
    name = tool["name"]
    method = tool.get("method", "GET").upper()
    path = tool.get("path", "/")
    base_url = tool.get("base_url", "")
    description = tool.get("description", f"Execute {name}")
    params = tool.get("params", [])

    sig_params = _build_signature_params(params)
    url_expr = _build_url_expression(path)

    body_params = [p for p in params if p.get("source") == "body"]
    query_params = [p for p in params if p.get("source") == "query"]

    # Build function signature string
    if sig_params:
        if len(sig_params) == 1:
            sig_str = sig_params[0]
        else:
            inner = ",\n    ".join(sig_params)
            sig_str = f"\n    {inner},\n"
    else:
        sig_str = ""

    # Build HTTP method call arguments
    method_lower = method.lower()
    http_args_parts = ["url, "]
    if body_params:
        http_args_parts.append("json=body, ")
    if query_params:
        http_args_parts.append("params=qs, ")
    http_args_parts.append("headers=auth")
    http_args = "".join(http_args_parts)

    # Assemble function body lines (each already at 4-space indent)
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

    body_lines.append("    auth = await get_auth_headers(TABBY_PROFILE_ID)")
    body_lines.append(f"    url = {url_expr}")
    body_lines.append("    async with httpx.AsyncClient() as client:")
    body_lines.append(f"        resp = await client.{method_lower}({http_args})")
    body_lines.append("        resp.raise_for_status()")
    body_lines.append("        try:")
    body_lines.append("            return resp.json()")
    body_lines.append("        except Exception:")
    body_lines.append('            return {"status": resp.status_code, "text": resp.text}')

    func_body = "\n".join(body_lines)

    source = (
        f'"""Auto-generated operation: {name}\n'
        f"Method: {method}\n"
        f"Path: {path}\n"
        '"""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "import httpx\n"
        "from noui_runtime.auth import get_auth_headers\n"
        "\n"
        f'BASE_URL = "{base_url}"\n'
        f'TABBY_PROFILE_ID = "{tabby_profile_id}"\n'
        "\n"
        "\n"
        f"async def execute({sig_str}) -> dict:\n"
        f'    """{description}"""\n'
        f"{func_body}\n"
    )

    return source
