"""Regression tests for compiler/mcp/auth_adapter.py.

Critical invariants:
- Generated auth.py must NOT contain the old /runtime/credentials/{profile_id} route.
- Generated auth.py MUST use POST /auth/agent-token + POST /credentials/request.
- Generated auth.py MUST load dotenv from the noui root.
- Generated auth.py MUST expose resolve_auth() (new) and get_auth_headers() (compat shim).
- Static secret strategy must fail clearly with a human-readable message when env var missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add noui root to sys.path so compiler imports work without installation
_NOUI_ROOT = Path(__file__).resolve().parent.parent
if str(_NOUI_ROOT) not in sys.path:
    sys.path.insert(0, str(_NOUI_ROOT))

from compiler.mcp.auth_adapter import generate_auth_adapter


def _generated() -> str:
    return generate_auth_adapter("http://localhost:8080")


class TestDeprecatedRouteNotGenerated:
    """Phase 1 regression: old /runtime/credentials/{profile_id} must not appear."""

    def test_no_runtime_credentials_route(self) -> None:
        src = _generated()
        assert "/runtime/credentials/" not in src, (
            "Generated auth.py must not use the deprecated GET /runtime/credentials/{profile_id} route"
        )

    def test_no_get_runtime_credentials(self) -> None:
        src = _generated()
        assert "client.get" not in src or "runtime/credentials" not in src, (
            "Generated auth.py must not do GET /runtime/credentials/"
        )


class TestCorrectTabbyFlow:
    """Generated auth.py must use the 2-step Tabby credential flow."""

    def test_uses_agent_token_endpoint(self) -> None:
        src = _generated()
        assert "/auth/agent-token" in src, (
            "Generated auth.py must POST /auth/agent-token to exchange client credentials"
        )

    def test_uses_credentials_request_endpoint(self) -> None:
        src = _generated()
        assert "/credentials/request" in src, (
            "Generated auth.py must POST /credentials/request to fetch live credentials"
        )

    def test_credentials_request_uses_profile_slug_field(self) -> None:
        """The credentials/request call must pass profile_id (slug, not DB UUID)."""
        src = _generated()
        assert '"profile_id"' in src or "'profile_id'" in src, (
            "Generated auth.py must pass profile_id (the slug) to /credentials/request"
        )


class TestEnvVarLoading:
    """Generated auth.py must load noui/.env and read client credentials from env."""

    def test_loads_dotenv(self) -> None:
        src = _generated()
        assert "load_dotenv" in src or "dotenv" in src, (
            "Generated auth.py must call load_dotenv() to pick up noui/.env"
        )

    def test_reads_client_id(self) -> None:
        src = _generated()
        assert "TABBY_CLIENT_ID" in src, (
            "Generated auth.py must read TABBY_CLIENT_ID from environment"
        )

    def test_reads_client_secret(self) -> None:
        src = _generated()
        assert "TABBY_CLIENT_SECRET" in src, (
            "Generated auth.py must read TABBY_CLIENT_SECRET from environment"
        )

    def test_missing_credentials_diagnostic(self) -> None:
        """Missing client credentials should produce a human-readable error, not a raw exception."""
        src = _generated()
        assert "noui tabby setup" in src or "TABBY_CLIENT_ID" in src, (
            "Generated auth.py must include guidance when TABBY_CLIENT_ID/SECRET are missing"
        )


class TestResolveAuthFunction:
    """Generated auth.py must expose resolve_auth() as the primary entry point."""

    def test_resolve_auth_defined(self) -> None:
        src = _generated()
        assert "async def resolve_auth()" in src, (
            "Generated auth.py must define resolve_auth() as the primary auth entry point"
        )

    def test_get_auth_headers_compat_shim(self) -> None:
        """get_auth_headers(profile_id) must still exist for backward compatibility."""
        src = _generated()
        assert "async def get_auth_headers(" in src, (
            "Generated auth.py must keep get_auth_headers() as a legacy shim"
        )


class TestStaticSecretStrategy:
    """resolve_auth() must support static_secret_header strategy via auth_plan.json."""

    def test_static_secret_header_handler(self) -> None:
        src = _generated()
        assert "static_secret_header" in src, (
            "Generated auth.py must handle static_secret_header strategy"
        )

    def test_missing_env_var_error_message(self) -> None:
        """Missing secret env var must produce a clear error, not a traceback."""
        src = _generated()
        assert "Missing required secret" in src or "missing" in src.lower(), (
            "Generated auth.py must raise a clear error when a required secret env var is absent"
        )

    def test_reads_auth_plan_json(self) -> None:
        src = _generated()
        assert "auth_plan.json" in src, "Generated auth.py must load strategy from auth_plan.json"


class TestTabbyApiHostConfig:
    """The baked-in Tabby host must be overridable via env var."""

    def test_custom_host_baked_in(self) -> None:
        src = generate_auth_adapter("http://tabby.internal:9090")
        assert "http://tabby.internal:9090" in src, (
            "Custom tabby_api_host must appear in the generated file"
        )

    def test_env_var_override(self) -> None:
        src = _generated()
        # Must prefer TABBY_API_URL or TABBY_API_HOST env vars
        assert "TABBY_API_URL" in src or "TABBY_API_HOST" in src, (
            "Generated auth.py must allow overriding TABBY_API_HOST via environment"
        )
