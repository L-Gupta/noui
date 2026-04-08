"""NoUI MCP server generator.

compile_workflow(...) is the single public entry point. It:

1. Converts the captured HAR into tool definitions (via har_to_tools).
2. Writes a complete FastMCP server tree to output_dir:
       server.py
       tools.json
       manifest.json
       API.md
       noui_runtime/auth.py
       operations/<tool_name>.py   (one file per tool)
3. Returns the manifest dict.
"""

from __future__ import annotations

import json
import re
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from compiler.mcp.api_doc_generator import generate_api_markdown
from compiler.mcp.har_to_tools import har_to_tool_defs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compile_workflow(
    *,
    session_id: str,
    session_name: str,
    app_slug: str,
    tabby_profile_id: str,
    har: dict,
    click_events: list[dict],  # noqa: ARG001 – reserved for future ranking
    url_events: list[dict],  # noqa: ARG001 – reserved for future ranking
    output_dir: str,
) -> dict:
    """Compile a recorded workflow session into a runnable FastMCP server.

    Returns the manifest dict (same content as manifest.json).
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # Derive stable identifiers
    server_id = f"{app_slug}-{session_id[:8]}"
    app_name = _slug_to_title(app_slug)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── 1. Convert HAR → tool_defs ────────────────────────────────────────────
    tool_defs = har_to_tool_defs(
        har,
        workflow_name=session_name,
        tabby_profile_id=tabby_profile_id,
    )

    # ── 2. Write noui_runtime/auth.py ─────────────────────────────────────────
    runtime_dir = out_path / "noui_runtime"
    runtime_dir.mkdir(exist_ok=True)
    (runtime_dir / "__init__.py").write_text("", encoding="utf-8")
    (runtime_dir / "auth.py").write_text(_AUTH_PY, encoding="utf-8")

    # ── 3. Write operations/*.py ──────────────────────────────────────────────
    ops_dir = out_path / "operations"
    ops_dir.mkdir(exist_ok=True)
    (ops_dir / "__init__.py").write_text("", encoding="utf-8")

    op_files: list[str] = []
    for td in tool_defs:
        op_src = _render_operation(td, tabby_profile_id=tabby_profile_id)
        op_file = ops_dir / f"{td['name']}.py"
        op_file.write_text(op_src, encoding="utf-8")
        op_files.append(f"operations/{td['name']}.py")

    # ── 4. Write server.py ────────────────────────────────────────────────────
    server_src = _render_server(
        app_name=app_name,
        tool_defs=tool_defs,
    )
    (out_path / "server.py").write_text(server_src, encoding="utf-8")

    # ── 5. Write tools.json ───────────────────────────────────────────────────
    (out_path / "tools.json").write_text(
        json.dumps(tool_defs, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # ── 6. Write API.md ───────────────────────────────────────────────────────
    api_md = generate_api_markdown(
        server_id=server_id,
        app_name=app_name,
        app_slug=app_slug,
        workflow_name=session_name,
        tool_defs=tool_defs,
        tabby_profile_id=tabby_profile_id,
        generated_at=generated_at,
    )
    (out_path / "API.md").write_text(api_md, encoding="utf-8")

    # ── 7. Build manifest ─────────────────────────────────────────────────────
    all_files = [
        "server.py",
        "tools.json",
        "noui_runtime/auth.py",
        *op_files,
        "API.md",
    ]

    manifest_tools = [
        {
            "name": td["name"],
            "description": td["description"],
            "method": td["method"],
            "path": td["path"],
            "module": f"operations/{td['name']}.py",
        }
        for td in tool_defs
    ]

    manifest: dict = {
        "schema_version": "1",
        "server_id": server_id,
        "app": {
            "name": app_name,
            "slug": app_slug,
        },
        "workflow": {
            "id": server_id,
            "name": session_name,
            "workflow_session_id": session_id,
        },
        "auth": {
            "tabby_profile_id": tabby_profile_id or None,
            "requires_auth": bool(tabby_profile_id),
        },
        "runtime": {
            "type": "fastmcp",
            "entrypoint": "server.py",
            "transport": "stdio",
            "direct_execution": True,
        },
        "tools": manifest_tools,
        "artifacts": {
            "server_file": "server.py",
            "tools_file": "tools.json",
            "files": all_files,
            "api_docs_file": "API.md",
        },
        "generation": {
            "generated_at": generated_at,
            "generator": "noui",
            "generator_version": "v1",
        },
    }

    (out_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return manifest


# ---------------------------------------------------------------------------
# Code renderers
# ---------------------------------------------------------------------------

_AUTH_PY = textwrap.dedent(
    '''\
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
    '''
)


def _render_server(*, app_name: str, tool_defs: list[dict]) -> str:
    lines: list[str] = [
        f'"""Auto-generated FastMCP server: {app_name}',
        "Generated by NoUI v1",
        '"""',
        "from __future__ import annotations",
        "",
        "from mcp.server.fastmcp import FastMCP",
        "",
    ]

    # Imports
    for td in tool_defs:
        n = td["name"]
        lines.append(f"from operations import {n} as _op_{n}")

    lines += [
        "",
        "",
        f'mcp = FastMCP("{app_name}")',
        "",
    ]

    # Tool registrations
    for td in tool_defs:
        n = td["name"]
        params = td.get("params", [])
        sig_parts = _py_signature(params)
        call_parts = ", ".join(f"{p['name']}={p['name']}" for p in params)
        desc = td.get("description", "").replace('"""', "'''")

        lines.append("")
        lines.append("@mcp.tool()")
        if sig_parts:
            sig = f"async def {n}(\n    {',\n    '.join(sig_parts)},\n) -> dict:"
        else:
            sig = f"async def {n}() -> dict:"
        lines.append(sig)
        lines.append(f'    """{desc}"""')
        if call_parts:
            lines.append(f"    return await _op_{n}.execute({call_parts})")
        else:
            lines.append(f"    return await _op_{n}.execute()")

    lines += [
        "",
        "",
        'if __name__ == "__main__":',
        "    mcp.run()",
        "",
    ]
    return "\n".join(lines)


def _render_operation(td: dict, *, tabby_profile_id: str) -> str:
    name = td["name"]
    method = td["method"].lower()
    path_template = td["path"]
    base_url = td.get("base_url", "")
    content_type = td.get("request_content_type", "")
    params: list[dict] = td.get("params", [])
    auth_headers: list[str] = td.get("auth_headers", [])
    auth_cookies: list[str] = td.get("auth_cookies", [])
    description = td.get("description", "")

    needs_auth = bool(tabby_profile_id or auth_headers or auth_cookies)

    lines: list[str] = [
        f'"""Auto-generated operation: {name}',
        f"Method: {method.upper()}",
        f"Path: {path_template}",
        '"""',
        "",
        "from __future__ import annotations",
        "",
    ]

    if needs_auth:
        lines += [
            "import json",
            "import pathlib",
            "",
            "import httpx",
            "",
            "from noui_runtime.auth import get_auth_headers",
            "",
            f"BASE_URL = {base_url!r}",
            "_MANIFEST = json.loads((pathlib.Path(__file__).parent.parent / 'manifest.json').read_text())",
            "TABBY_PROFILE_ID: str | None = _MANIFEST.get('auth', {}).get('tabby_profile_id')",
        ]
    else:
        lines += [
            "import httpx",
            "",
            f"BASE_URL = {base_url!r}",
        ]

    lines.append("")
    lines.append("")

    # Build function signature
    sig_parts = _py_signature(params)
    desc_safe = description.replace('"""', "'''")

    if sig_parts:
        lines.append(
            f"async def execute(\n    {',\\n    '.join(sig_parts)},\n) -> dict:"
        )
    else:
        lines.append("async def execute() -> dict:")
    lines.append(f'    """{desc_safe}"""')

    # Build URL
    path_params = [p for p in params if p.get("source") == "path"]
    body_params = [p for p in params if p.get("source") in ("body", None, "")]
    query_params = [p for p in params if p.get("source") == "query"]

    # Build URL string
    url_expr = f'f"{base_url}{_path_to_fstring(path_template)}"'
    lines.append(f"    url = {url_expr}")

    # Build request body / query
    if body_params and method in ("post", "put", "patch"):
        body_dict = ", ".join(f"{repr(p['name'])}: {p['name']}" for p in body_params)
        if "json" in content_type:
            lines.append(f"    body = {{{body_dict}}}")
        else:
            lines.append(f"    data = {{{body_dict}}}")

    if query_params:
        q_dict = ", ".join(f"{repr(p['name'])}: {p['name']}" for p in query_params)
        lines.append(f"    params = {{{q_dict}}}")

    # Auth
    if needs_auth:
        lines.append("    auth_hdrs = await get_auth_headers(TABBY_PROFILE_ID) if TABBY_PROFILE_ID else {}")
        lines.append("    headers = {**auth_hdrs}")
    else:
        lines.append("    headers = {}")

    # Additional static headers (non-auth)
    request_headers: list[dict] = td.get("request_headers", [])
    for h in request_headers:
        hname = h.get("name", "")
        hval = h.get("value", "")
        if hname.lower() not in ("authorization", "cookie"):
            lines.append(f"    headers[{hname!r}] = {hval!r}")

    # HTTP call
    lines.append(f"    async with httpx.AsyncClient() as client:")

    call_kwargs: list[str] = ["url", "headers=headers"]
    if query_params:
        call_kwargs.append("params=params")
    if body_params and method in ("post", "put", "patch"):
        if "json" in content_type:
            call_kwargs.append("json=body")
        else:
            call_kwargs.append("data=data")

    call_args = ", ".join(call_kwargs)
    lines.append(f"        resp = await client.{method}({call_args})")
    lines.append("        resp.raise_for_status()")
    lines.append("        try:")
    lines.append("            return resp.json()")
    lines.append("        except Exception:")
    lines.append("            return {\"status\": resp.status_code, \"text\": resp.text}")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Python code helpers
# ---------------------------------------------------------------------------


def _py_signature(params: list[dict]) -> list[str]:
    """Return a list of Python parameter declaration strings."""
    parts: list[str] = []
    required = [p for p in params if p.get("required", True)]
    optional = [p for p in params if not p.get("required", True)]
    for p in required:
        ptype = _py_type(p.get("type", "string"))
        parts.append(f"{p['name']}: {ptype}")
    for p in optional:
        ptype = _py_type(p.get("type", "string"))
        default = _py_default(p.get("type", "string"))
        parts.append(f"{p['name']}: {ptype} = {default}")
    return parts


def _py_type(t: str) -> str:
    return {"int": "int", "integer": "int", "bool": "bool", "boolean": "bool", "float": "float"}.get(
        t.lower(), "str"
    )


def _py_default(t: str) -> str:
    return {"int": "0", "integer": "0", "bool": "False", "boolean": "False", "float": "0.0"}.get(
        t.lower(), '""'
    )


def _path_to_fstring(path_template: str) -> str:
    """Convert /posts/{id} → /posts/{id} (already valid f-string interpolation)."""
    return path_template


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _slug_to_title(slug: str) -> str:
    """Convert a kebab-case slug to a Title Case name."""
    return " ".join(word.capitalize() for word in re.split(r"[-_]+", slug))
