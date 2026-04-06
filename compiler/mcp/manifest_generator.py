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
) -> dict:
    """Generate the server manifest dict.

    Schema version 1. See project context for full schema.
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

    return {
        "schema_version": "1",
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
            "tabby_profile_id": tabby_profile_id or None,
            "requires_auth": bool(tabby_profile_id),
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
            "generator_version": "v1",
        },
    }
