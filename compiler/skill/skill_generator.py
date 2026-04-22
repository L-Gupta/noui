"""NoUI Skill generator.

`compile_workflow_to_skill(...)` is the single public entry point. Mirrors
`compiler.mcp.server_generator.compile_workflow` so the backend export endpoint
can branch on --as mcp|skill|both without reshaping its inputs.

Output tree:

    <output_dir>/
        SKILL.md
        manifest.json
        API.md
        auth_plan.json          (when auth is required)
        noui_runtime/
            __init__.py
            auth.py             (identical to the MCP-server runtime)
        operations/
            __init__.py
            <op>.py             (one standalone CLI script per tool)
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from compiler.mcp.api_doc_generator import generate_api_markdown
from compiler.mcp.auth_plan import generate_auth_plan
from compiler.mcp.har_to_tools import har_to_tool_defs
from compiler.runtime.auth_adapter import generate_auth_adapter
from compiler.runtime.cdp_adapter import generate_cdp_adapter
from compiler.skill.operation_generator import render_skill_operation
from compiler.skill.skill_md_generator import render_skill_md

_VALID_EXECUTION_MODES = ("cdp", "http")


def compile_workflow_to_skill(
    *,
    session_id: str,
    session_name: str,
    app_slug: str,
    tabby_profile_id: str,
    har: dict,
    click_events: list[dict],  # noqa: ARG001 – reserved for future ranking
    url_events: list[dict],  # noqa: ARG001 – reserved for future ranking
    output_dir: str,
    profile_slug: str = "",
    profile_db_id: str = "",
    description_override: str = "",
    execution_mode: str = "cdp",
) -> dict:
    """Compile a recorded workflow session into an installable Claude Code skill.

    See `compile_workflow` in compiler.mcp.server_generator for `execution_mode`
    semantics — the two compilers stay in lockstep.

    Returns the manifest dict (same content as manifest.json).
    """
    if execution_mode not in _VALID_EXECUTION_MODES:
        raise ValueError(
            f"Invalid execution_mode {execution_mode!r}. Expected one of {_VALID_EXECUTION_MODES}."
        )

    from backend.config import settings as _settings

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    effective_slug = profile_slug or tabby_profile_id or ""
    skill_id = app_slug  # skills use app_slug directly; re-export overwrites silently
    app_name = _slug_to_title(app_slug)
    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. Tool definitions (shared with MCP compiler)
    tool_defs = har_to_tool_defs(
        har,
        workflow_name=session_name,
        tabby_profile_id=effective_slug,
    )

    # 2. Auth plan (shared)
    auth_headers_seen: list[str] = []
    auth_cookies_seen: list[str] = []
    for td in tool_defs:
        for h in td.get("auth_headers", []):
            if h not in auth_headers_seen:
                auth_headers_seen.append(h)
        for c in td.get("auth_cookies", []):
            if c not in auth_cookies_seen:
                auth_cookies_seen.append(c)

    has_auth = bool(effective_slug or auth_headers_seen or auth_cookies_seen)

    auth_plan: dict = {}
    if has_auth:
        auth_info = {
            "has_auth_headers": bool(auth_headers_seen),
            "has_cookies": bool(auth_cookies_seen),
            "has_csrf": any("csrf" in h.lower() or "xsrf" in h.lower() for h in auth_headers_seen),
            "auth_header_names": auth_headers_seen,
            "csrf_header_names": [
                h for h in auth_headers_seen if "csrf" in h.lower() or "xsrf" in h.lower()
            ],
            "set_cookie_names": auth_cookies_seen,
            "auth_domains": [],
        }
        auth_plan = generate_auth_plan(
            har=har,
            auth_info=auth_info,
            profile_slug=effective_slug,
            profile_db_id=profile_db_id,
            app_slug=app_slug,
        )

    # 3. noui_runtime/auth.py (shared template, identical bytes for both outputs)
    runtime_dir = out_path / "noui_runtime"
    runtime_dir.mkdir(exist_ok=True)
    (runtime_dir / "__init__.py").write_text("", encoding="utf-8")
    (runtime_dir / "auth.py").write_text(
        generate_auth_adapter(_settings.tabby_api_host), encoding="utf-8"
    )
    if execution_mode == "cdp":
        (runtime_dir / "cdp.py").write_text(generate_cdp_adapter(), encoding="utf-8")

    # 4. pyproject.toml + .python-version — per-skill Python environment (retro D1).
    # Needs httpx for any CDP or HTTP operation; websockets only for CDP mode.
    pyproject_deps = ['"httpx>=0.27"']
    if execution_mode == "cdp":
        pyproject_deps.append('"websockets>=12"')
    pyproject_toml = (
        f"[project]\n"
        f'name = "{skill_id}"\n'
        f'version = "0.1.0"\n'
        f'description = "NoUI-generated skill for {app_name}."\n'
        f'requires-python = ">=3.11"\n'
        f"dependencies = [\n"
        + "".join(f"    {d},\n" for d in pyproject_deps)
        + f"]\n"
        f"\n"
        f"[tool.uv]\n"
        f"package = false\n"
    )
    (out_path / "pyproject.toml").write_text(pyproject_toml, encoding="utf-8")
    (out_path / ".python-version").write_text("3.11\n", encoding="utf-8")

    # 5. operations/*.py (skill-specific rendering with CLI wrapper)
    ops_dir = out_path / "operations"
    ops_dir.mkdir(exist_ok=True)
    (ops_dir / "__init__.py").write_text("", encoding="utf-8")

    op_files: list[str] = []
    op_entries: list[dict] = []
    for td in tool_defs:
        op_src = render_skill_operation(td, auth_plan=auth_plan, execution_mode=execution_mode)
        op_file = ops_dir / f"{td['name']}.py"
        op_file.write_text(op_src, encoding="utf-8")
        op_files.append(f"operations/{td['name']}.py")
        op_entries.append(
            {
                "name": td["name"],
                "description": td.get("description", td["name"]),
                "module": f"operations/{td['name']}.py",
                "entry": "execute",
                "method": td["method"],
                "path": td["path"],
                "args": [
                    {
                        "name": p["name"],
                        "type": p.get("type", "string"),
                        "required": bool(p.get("required", True)),
                        **(
                            {"default": p["default"]}
                            if not p.get("required", True) and "default" in p
                            else {}
                        ),
                    }
                    for p in td.get("params", [])
                ],
            }
        )

    # 6. SKILL.md (frontmatter + body — what Claude loads when intent matches)
    skill_md = render_skill_md(
        skill_id=skill_id,
        app_name=app_name,
        app_slug=app_slug,
        workflow_name=session_name,
        tool_defs=tool_defs,
        auth_plan=auth_plan,
        profile_slug=effective_slug,
        description_override=description_override,
        python_executable=".venv/bin/python",
    )
    (out_path / "SKILL.md").write_text(skill_md, encoding="utf-8")

    # 6. API.md (reuse MCP generator — format-identical)
    api_md = generate_api_markdown(
        server_id=skill_id,
        app_name=app_name,
        app_slug=app_slug,
        workflow_name=session_name,
        tool_defs=tool_defs,
        tabby_profile_id=effective_slug,
        generated_at=generated_at,
    )
    (out_path / "API.md").write_text(api_md, encoding="utf-8")

    # 7. auth_plan.json (when auth is required)
    if auth_plan:
        if effective_slug:
            auth_plan["profile_slug"] = effective_slug
            if "tabby_export" in auth_plan:
                auth_plan["tabby_export"]["runtime_identifier"] = effective_slug
        (out_path / "auth_plan.json").write_text(
            json.dumps(auth_plan, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # 9. manifest.json
    all_files = [
        "SKILL.md",
        "API.md",
        "pyproject.toml",
        ".python-version",
        "noui_runtime/__init__.py",
        "noui_runtime/auth.py",
        "operations/__init__.py",
        *op_files,
    ]
    if execution_mode == "cdp":
        all_files.append("noui_runtime/cdp.py")
    if auth_plan:
        all_files.append("auth_plan.json")

    auth_strategy = auth_plan.get("strategy", "") if auth_plan else ""
    resolved_auth_strategy = auth_strategy or ("tabby_credentials" if has_auth else None)
    execution_strategy = (
        "cdp_browser_session" if execution_mode == "cdp" else resolved_auth_strategy
    )

    manifest: dict = {
        "schema_version": "1",
        "skill_id": skill_id,
        "app": {
            "name": app_name,
            "slug": app_slug,
        },
        "workflow": {
            "id": skill_id,
            "name": session_name,
            "workflow_session_id": session_id,
        },
        "auth": {
            "requires_auth": has_auth,
            "profile_slug": effective_slug or None,
            "profile_db_id": profile_db_id or None,
            "strategy": resolved_auth_strategy,
            "execution_strategy": execution_strategy,
            "auth_plan_file": "auth_plan.json" if auth_plan else None,
        },
        "runtime": {
            "type": "claude-code-skill",
            "entrypoint": "SKILL.md",
            "operation_style": "subprocess-cli",
            "python": ">=3.11",
            "python_executable": ".venv/bin/python",
        },
        "operations": op_entries,
        "artifacts": {
            "skill_file": "SKILL.md",
            "files": all_files,
            "api_docs_file": "API.md",
        },
        "generation": {
            "generated_at": generated_at,
            "generator": "noui",
            "generator_version": "v1-skill",
        },
    }

    (out_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return manifest


def _slug_to_title(slug: str) -> str:
    return " ".join(word.capitalize() for word in re.split(r"[-_]+", slug))
