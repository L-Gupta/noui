"""Generate the JSON manifest for a compiled MCP server.

Pure functions — no web framework or DB dependencies.
"""

from __future__ import annotations

from datetime import datetime, timezone


def generate_manifest(
    server_id: str,
    app_name: str,
    app_slug: str,
    workflow_session_id: str,
    workflow_name: str,
    tabby_profile_id: str,
    tool_defs: list[dict],
    output_files: list[str],
    profile_slug: str = "",
    profile_db_id: str = "",
    auth_strategy: str = "",
) -> dict:
    """Generate the server manifest dict.

    Schema version 2. Extends v1 auth section with profile_slug, profile_db_id,
    strategy, and auth_plan_file for the AuthPlan verification loop.

    Args:
        tabby_profile_id: Legacy field — kept for backward compatibility.
            Prefer profile_slug for runtime credential requests.
        profile_slug: Tabby profile slug used for runtime credential requests
            (POST /credentials/request). Takes precedence over tabby_profile_id.
        profile_db_id: Tabby profile DB UUID for admin/version operations.
        auth_strategy: Auth strategy name ("tabby_credentials", "static_secret_header",
            or "" for unauthenticated).
    """
    tools = [
        {
            "name": td["name"],
            "description": td.get("description", ""),
            "method": td.get("method", "GET"),
            "path": td.get("path", "/"),
            "module": f"operations/{td['name']}.py",
        }
        for td in tool_defs
    ]

    # Derive the workflow id from the slug + session short id
    workflow_id = f"{app_slug}-{workflow_session_id[:8]}"

    # Resolve the effective profile slug: prefer explicit profile_slug,
    # fall back to tabby_profile_id (legacy), then app_slug as last resort.
    effective_slug = profile_slug or tabby_profile_id or ""
    requires_auth = bool(effective_slug or auth_strategy)
    has_auth_plan = bool(effective_slug or auth_strategy)

    return {
        "schema_version": "2",
        "server_id": server_id,
        "app": {
            "name": app_name,
            "slug": app_slug,
        },
        "workflow": {
            "id": workflow_id,
            "name": workflow_name,
            "workflow_session_id": workflow_session_id,
        },
        "auth": {
            # Legacy field — kept for tools that read v1 manifests
            "tabby_profile_id": tabby_profile_id or None,
            "requires_auth": requires_auth,
            # New v2 fields
            "profile_slug": effective_slug or None,
            "profile_db_id": profile_db_id or None,
            "strategy": auth_strategy or (
                "tabby_credentials" if requires_auth else None
            ),
            "auth_plan_file": "auth_plan.json" if has_auth_plan else None,
        },
        "runtime": {
            "type": "fastmcp",
            "entrypoint": "server.py",
            "transport": "stdio",
            "direct_execution": True,
        },
        "tools": tools,
        "artifacts": {
            "server_file": "server.py",
            "tools_file": "tools.json",
            "files": output_files,
        },
        "generation": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generator": "noui",
            "generator_version": "v2",
        },
    }
