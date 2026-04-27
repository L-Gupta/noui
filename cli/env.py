"""Environment resolution for the NoUI CLI.

The CLI historically read ``TABBY_API_HOST`` straight from ``os.environ`` at
import time, which meant a ``.env`` file in the repository root (the canonical
location documented in ``.env.example`` and consumed by ``backend/config.py``)
was silently ignored when running ``noui`` subcommands. The result was a
confusing setup-time mismatch: the backend would talk to the user's configured
Tabby host while the CLI kept reaching for ``http://localhost:8080``.

This module centralises that resolution. It exposes two pure helpers:

* :func:`load_env_files` populates ``os.environ`` from the repo-root ``.env``.
  It is idempotent, never overrides values that already live in the process
  environment, and is a no-op when ``python-dotenv`` is not installed.

* :func:`resolve_tabby_api_host` returns a normalised Tabby API base URL,
  applying the same default as ``backend/config.py`` and adding a missing
  scheme when callers wrote ``localhost:8080`` instead of
  ``http://localhost:8080``.

Keeping these as side-effect-free functions means tests can exercise them
without importing the rest of ``cli/main.py`` (which has substantial
import-time work).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

DEFAULT_TABBY_API_HOST = "http://localhost:8080"

_CLI_DIR = Path(__file__).resolve().parent
_NOUI_ROOT = _CLI_DIR.parent


def load_env_files(noui_root: Path | None = None) -> None:
    """Populate ``os.environ`` from the repo-root ``.env`` if present.

    Existing process-environment values always win (``override=False``),
    matching the behaviour of :mod:`backend.config`. Safe to call more than
    once. A missing ``python-dotenv`` install is treated as a no-op so the
    CLI keeps working in minimal environments.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    root = noui_root if noui_root is not None else _NOUI_ROOT
    env_path = root / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)


def resolve_tabby_api_host(env: Mapping[str, str] | None = None) -> str:
    """Return a normalised Tabby API base URL.

    Resolution order:

    1. ``TABBY_API_HOST`` from the supplied mapping (defaults to
       :data:`os.environ`, which callers are expected to have populated via
       :func:`load_env_files`).
    2. :data:`DEFAULT_TABBY_API_HOST` when the value is missing or empty.

    The returned string is guaranteed to:

    * include an explicit scheme (``http://`` is prepended when absent so
      common abbreviations like ``localhost:8080`` keep working);
    * have no trailing slash, so callers can safely concatenate paths
      starting with ``/``.
    """
    src = env if env is not None else os.environ
    raw = (src.get("TABBY_API_HOST") or "").strip()
    if not raw:
        raw = DEFAULT_TABBY_API_HOST
    if "://" not in raw:
        raw = "http://" + raw
    return raw.rstrip("/")
