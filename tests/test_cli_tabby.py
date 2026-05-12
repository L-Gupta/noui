"""Unit tests for Tabby-related CLI helpers and commands.

Covers behaviour added in the "improve Tabby CLI failure messages" PR:

1. ``_tabby_http`` translates ``urllib.error.HTTPError`` and connection /
   timeout failures into a single ``RuntimeError`` with an actionable message.
2. ``_get_sessions`` swallows errors by default but propagates them when
   ``raise_on_error=True`` (used by the login-validate polling loop).
3. ``cmd_login_validate`` surfaces the last polling error on timeout instead
   of dropping it silently.
4. ``cmd_login_register`` prints the human-friendly profile slug as
   ``Tabby profile ID`` rather than repeating the database UUID.
"""

from __future__ import annotations

import io
import json
import socket
import sys
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_NOUI_ROOT = Path(__file__).resolve().parent.parent
if str(_NOUI_ROOT) not in sys.path:
    sys.path.insert(0, str(_NOUI_ROOT))

from cli import main as cli_main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for ``urllib.request.urlopen`` context-manager output."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


@contextmanager
def _patched_urlopen(side_effect):
    """Patch ``urllib.request.urlopen`` for the duration of the test."""
    with patch.object(cli_main.urllib.request, "urlopen", side_effect=side_effect) as p:
        yield p


def _make_http_error(code: int, body: str = "{}") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://localhost:8080/whatever",
        code=code,
        msg="test",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body.encode()),
    )


# ---------------------------------------------------------------------------
# 1. _tabby_http transport / HTTP error handling
# ---------------------------------------------------------------------------


class TestTabbyHttpErrors:
    def test_happy_path_returns_parsed_json(self) -> None:
        with _patched_urlopen(side_effect=lambda *_a, **_k: _FakeResponse({"ok": True})):
            result = cli_main._tabby_http("GET", "/health/live")
        assert result == {"ok": True}

    def test_http_error_preserves_status_and_body(self) -> None:
        # Regression: existing behaviour must keep working unchanged.
        err = _make_http_error(401, '{"detail":"unauthorized"}')
        with _patched_urlopen(side_effect=err), pytest.raises(RuntimeError) as exc_info:
            cli_main._tabby_http("GET", "/admin/profiles", token="bad")
        msg = str(exc_info.value)
        assert "HTTP 401" in msg
        assert "unauthorized" in msg
        assert "/admin/profiles" in msg

    def test_connection_refused_is_actionable(self) -> None:
        # Simulate the Tabby API not running at all.
        err = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        with _patched_urlopen(side_effect=err), pytest.raises(RuntimeError) as exc_info:
            cli_main._tabby_http("GET", "/health/live")
        msg = str(exc_info.value)
        assert "Cannot reach Tabby" in msg
        assert cli_main.TABBY_API_HOST in msg
        assert "noui tabby status" in msg
        # The underlying OS error should be visible to the user.
        assert "Connection refused" in msg

    def test_dns_failure_is_actionable(self) -> None:
        # gaierror is the typical DNS-level failure surfaced as URLError.
        err = urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided"))
        with _patched_urlopen(side_effect=err), pytest.raises(RuntimeError) as exc_info:
            cli_main._tabby_http("GET", "/health/live")
        msg = str(exc_info.value)
        assert "Cannot reach Tabby" in msg
        assert "nodename" in msg or "servname" in msg

    def test_timeout_is_named_explicitly(self) -> None:
        # urllib wraps socket-level timeouts in URLError(reason=TimeoutError(...)).
        err = urllib.error.URLError(TimeoutError("timed out"))
        with _patched_urlopen(side_effect=err), pytest.raises(RuntimeError) as exc_info:
            cli_main._tabby_http("GET", "/health/live", timeout=7)
        msg = str(exc_info.value)
        assert "Cannot reach Tabby" in msg
        assert "timed out after 7s" in msg


# ---------------------------------------------------------------------------
# 2. _get_sessions raise_on_error toggle
# ---------------------------------------------------------------------------


class TestGetSessions:
    def test_default_swallows_errors(self) -> None:
        with patch.object(cli_main, "_tabby_http", side_effect=RuntimeError("boom")):
            assert cli_main._get_sessions("tok") == []

    def test_raise_on_error_propagates(self) -> None:
        with (
            patch.object(cli_main, "_tabby_http", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError, match="boom"),
        ):
            cli_main._get_sessions("tok", raise_on_error=True)

    def test_returns_list_payload(self) -> None:
        sessions = [{"id": "abc", "state": "HEALTHY"}]
        with patch.object(cli_main, "_tabby_http", return_value={"data": sessions}):
            assert cli_main._get_sessions("tok") == sessions


# ---------------------------------------------------------------------------
# 3. cmd_login_validate surfaces the last polling error on timeout
# ---------------------------------------------------------------------------


class TestLoginValidateErrorSurfacing:
    def _bundle_path(self, tmp_path: Path) -> Path:
        bundle = {
            "_provisioned": {
                "app_id": "app-uuid-1234",
                "profile_db_id": "db-uuid-5678",
                "profile_id": "my-app",
            }
        }
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps(bundle))
        return path

    def _drive_polling_loop(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        """Patch time so the 60s polling loop completes in two iterations."""
        ticks = iter([100.0, 100.5, 101.0, 200.0])  # last tick exits the loop
        used: list[float] = []

        def fake_time() -> float:
            t = next(ticks)
            used.append(t)
            return t

        monkeypatch.setattr(cli_main.time, "time", fake_time)
        monkeypatch.setattr(cli_main.time, "sleep", lambda _s: None)
        return used

    def test_timeout_includes_last_polling_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        bundle = self._bundle_path(tmp_path)
        monkeypatch.setattr(cli_main, "_tabby_alive", lambda: True)
        monkeypatch.setattr(cli_main, "_get_admin_token", lambda: "tok")
        monkeypatch.setattr(
            cli_main,
            "_get_sessions",
            lambda _t, raise_on_error=False: (_ for _ in ()).throw(
                RuntimeError("Cannot reach Tabby at http://localhost:8080 (Connection refused)")
            ),
        )
        self._drive_polling_loop(monkeypatch)

        rc = cli_main.cmd_login_validate(SimpleNamespace(bundle_file=str(bundle)))
        assert rc == 1
        out = capsys.readouterr().out
        assert "Last polling error" in out
        assert "Connection refused" in out

    def test_timeout_without_errors_keeps_classic_message(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # If polling succeeds but never finds a HEALTHY session, the existing
        # "noui tabby session ensure" hint must still be the message shown.
        bundle = self._bundle_path(tmp_path)
        monkeypatch.setattr(cli_main, "_tabby_alive", lambda: True)
        monkeypatch.setattr(cli_main, "_get_admin_token", lambda: "tok")
        monkeypatch.setattr(cli_main, "_get_sessions", lambda _t, raise_on_error=False: [])
        self._drive_polling_loop(monkeypatch)

        rc = cli_main.cmd_login_validate(SimpleNamespace(bundle_file=str(bundle)))
        assert rc == 1
        out = capsys.readouterr().out
        assert "Last polling error" not in out
        assert "noui tabby session ensure" in out


# ---------------------------------------------------------------------------
# 4. cmd_login_register prints the slug, not the DB UUID, as profile ID
# ---------------------------------------------------------------------------


class TestLoginRegisterOutput:
    def test_tabby_profile_id_is_the_slug(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        bundle = {
            "validation": {"generator_valid": True},
            "application_draft": {
                "name": "MyApp",
                "target_urls": ["https://example.com"],
                "login_config": {"credential_ref": "k8s:secret/tabby-my-app"},
            },
            "service_profile_draft": {
                "profile_id": "my-app",
                "version": "0.0.0",
            },
        }
        bundle_path = tmp_path / "bundle.json"
        bundle_path.write_text(json.dumps(bundle))

        monkeypatch.setattr(cli_main, "_tabby_alive", lambda: True)
        monkeypatch.setattr(cli_main, "_get_admin_token", lambda: "tok")
        monkeypatch.setattr(cli_main, "_load_cache", lambda: {})
        monkeypatch.setattr(cli_main, "_save_cache", lambda _c: None)

        def fake_http(method: str, path: str, body=None, token=None, timeout=15):  # noqa: ARG001
            if path == "/apps":
                return {"app_id": "app-uuid-1111"}
            if path == "/admin/profiles":
                return {"id": "db-uuid-2222"}
            raise AssertionError(f"unexpected call: {method} {path}")

        monkeypatch.setattr(cli_main, "_tabby_http", fake_http)

        rc = cli_main.cmd_login_register(SimpleNamespace(bundle_file=str(bundle_path)))
        assert rc == 0

        out = capsys.readouterr().out
        # The slug must appear next to "Tabby profile ID", and the DB UUID must
        # NOT show up there (it still appears next to "ServiceProfile DB ID").
        tabby_line = next(line for line in out.splitlines() if "Tabby profile ID" in line)
        assert "my-app" in tabby_line
        assert "db-uuid-2222" not in tabby_line
        assert "ServiceProfile DB ID" in out
