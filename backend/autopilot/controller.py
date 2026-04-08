"""Autopilot Recording Controller — helper functions.

These are pure helper functions used by the CLI and the skill.
The actual browser driving is done by Claude Code directly.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse


def normalize_request_fields(
    website_url: str,
    login_url: str = "",
    task_description: str = "",
    success_condition: str = "",
    stop_condition: str = "",
) -> dict:
    """Derive missing fields from the provided request.

    Returns a dict of the derived fields (only those that were empty).
    """
    updates: dict = {}

    if not login_url:
        parsed = urlparse(website_url)
        updates["login_url"] = f"{parsed.scheme}://{parsed.netloc}/login"

    if not stop_condition:
        updates["stop_condition"] = (
            "Stop after the primary action is complete. "
            "Do not click Send, Submit, Pay, Invite, Delete, or Publish."
        )

    if not success_condition and task_description:
        updates["success_condition"] = f"The described task is complete: {task_description}"

    return updates


def derive_app_slug(url: str) -> str:
    """Derive a short app slug from a URL."""
    hostname = urlparse(url).hostname or "app"
    slug = hostname.replace("www.", "").split(".")[0]
    return re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-") or "app"


def derive_workflow_name(task_description: str) -> str:
    """Derive a short workflow name from a task description."""
    words = task_description.split()[:6]
    name = " ".join(words)
    if len(task_description.split()) > 6:
        name += "..."
    return name
