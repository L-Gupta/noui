"""Tests for cli/env.py.

These exercise the env-loading and Tabby-host normalisation helpers in
isolation. They never import ``cli.main`` (which has side-effecting
import-time work) so they can manipulate ``os.environ`` and dotenv state
without disturbing the wider test suite.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_NOUI_ROOT = Path(__file__).resolve().parent.parent
if str(_NOUI_ROOT) not in sys.path:
    sys.path.insert(0, str(_NOUI_ROOT))

from cli.env import DEFAULT_TABBY_API_HOST, load_env_files, resolve_tabby_api_host

# ---------------------------------------------------------------------------
# resolve_tabby_api_host
# ---------------------------------------------------------------------------


class TestResolveTabbyApiHost:
    def test_default_when_unset(self) -> None:
        assert resolve_tabby_api_host(env={}) == DEFAULT_TABBY_API_HOST

    def test_default_when_blank(self) -> None:
        """Empty / whitespace-only values fall back to the default rather
        than producing an unusable bare 'http://' URL."""
        for raw in ("", "   ", "\t"):
            assert resolve_tabby_api_host(env={"TABBY_API_HOST": raw}) == DEFAULT_TABBY_API_HOST

    def test_explicit_value_passes_through(self) -> None:
        assert (
            resolve_tabby_api_host(env={"TABBY_API_HOST": "http://tabby.example:8080"})
            == "http://tabby.example:8080"
        )

    def test_https_scheme_preserved(self) -> None:
        assert (
            resolve_tabby_api_host(env={"TABBY_API_HOST": "https://tabby.example"})
            == "https://tabby.example"
        )

    def test_missing_scheme_gets_http_prefix(self) -> None:
        """A common user mistake — writing ``localhost:8080`` without a scheme.
        We normalise this so urllib.request doesn't raise an opaque
        ``unknown url type`` error downstream."""
        assert (
            resolve_tabby_api_host(env={"TABBY_API_HOST": "localhost:8080"})
            == "http://localhost:8080"
        )
        assert (
            resolve_tabby_api_host(env={"TABBY_API_HOST": "10.0.0.5:9000"})
            == "http://10.0.0.5:9000"
        )

    def test_trailing_slash_stripped(self) -> None:
        """Callers concatenate path strings starting with '/', so the host
        must not end with one — keeps the existing _tabby_http() shape valid."""
        assert (
            resolve_tabby_api_host(env={"TABBY_API_HOST": "http://localhost:8080/"})
            == "http://localhost:8080"
        )

    def test_surrounding_whitespace_stripped(self) -> None:
        """Common typo when copy-pasting from docs."""
        assert (
            resolve_tabby_api_host(env={"TABBY_API_HOST": "  http://localhost:8080  "})
            == "http://localhost:8080"
        )

    def test_defaults_to_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TABBY_API_HOST", "http://from-os-env:1234")
        assert resolve_tabby_api_host() == "http://from-os-env:1234"

    def test_default_constant_value(self) -> None:
        """Pin the default literal so it can't drift away from
        backend/config.py and .env.example without a deliberate change."""
        assert DEFAULT_TABBY_API_HOST == "http://localhost:8080"


# ---------------------------------------------------------------------------
# load_env_files
# ---------------------------------------------------------------------------


class TestLoadEnvFiles:
    def test_populates_missing_keys(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / ".env").write_text("TABBY_API_HOST=http://from-dotenv:8090\n")
        monkeypatch.delenv("TABBY_API_HOST", raising=False)
        load_env_files(tmp_path)
        assert resolve_tabby_api_host() == "http://from-dotenv:8090"

    def test_existing_env_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``override=False`` semantics — if the user already exported the
        variable in their shell, the dotenv value must NOT clobber it. This
        matches backend/config.py and is the principle of least surprise for
        anyone running ``TABBY_API_HOST=... noui ...``."""
        (tmp_path / ".env").write_text("TABBY_API_HOST=http://from-dotenv:8090\n")
        monkeypatch.setenv("TABBY_API_HOST", "http://from-shell:7000")
        load_env_files(tmp_path)
        assert resolve_tabby_api_host() == "http://from-shell:7000"

    def test_missing_dotenv_file_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no .env exists, nothing is added to os.environ and the
        default is used. Nothing should raise."""
        monkeypatch.delenv("TABBY_API_HOST", raising=False)
        load_env_files(tmp_path)
        assert resolve_tabby_api_host() == DEFAULT_TABBY_API_HOST

    def test_missing_dotenv_package_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If python-dotenv is not installed, the helper must silently
        no-op rather than crash the CLI on import."""
        (tmp_path / ".env").write_text("TABBY_API_HOST=http://from-dotenv:8090\n")
        monkeypatch.delenv("TABBY_API_HOST", raising=False)

        real_import = (
            __builtins__["__import__"]
            if isinstance(__builtins__, dict)
            else __builtins__.__import__
        )  # type: ignore[index]

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "dotenv":
                raise ImportError("simulated missing python-dotenv")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            load_env_files(tmp_path)

        assert resolve_tabby_api_host() == DEFAULT_TABBY_API_HOST

    def test_idempotent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Calling twice in the same process must not raise or change the
        resolved value."""
        (tmp_path / ".env").write_text("TABBY_API_HOST=http://from-dotenv:8090\n")
        monkeypatch.delenv("TABBY_API_HOST", raising=False)
        load_env_files(tmp_path)
        load_env_files(tmp_path)
        assert resolve_tabby_api_host() == "http://from-dotenv:8090"
