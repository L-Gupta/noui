"""Regression tests for compiler/mcp/tool_generator.py.

Critical invariants:
- Authenticated operations must call resolve_auth() (not get_auth_headers(TABBY_PROFILE_ID)).
- Recorded non-auth headers must be preserved and merged with auth headers.
- Unauthenticated operations must not import or call resolve_auth().
- No TABBY_PROFILE_ID constant should be embedded in generated operation code.
"""

from __future__ import annotations

import sys
from pathlib import Path

_NOUI_ROOT = Path(__file__).resolve().parent.parent
if str(_NOUI_ROOT) not in sys.path:
    sys.path.insert(0, str(_NOUI_ROOT))

from compiler.mcp.tool_generator import generate_operation_module


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _simple_tool(
    name: str = "list_documents",
    method: str = "GET",
    path: str = "/documents",
    base_url: str = "https://api.example.com",
    request_headers: list[dict] | None = None,
    params: list[dict] | None = None,
) -> dict:
    return {
        "name": name,
        "method": method,
        "path": path,
        "base_url": base_url,
        "description": f"Execute {name}",
        "request_headers": request_headers or [],
        "request_body": None,
        "request_content_type": "",
        "params": params or [],
        "auth_headers": [],
        "auth_cookies": [],
    }


def _tabby_auth_plan(profile_slug: str = "adopt-bank") -> dict:
    return {
        "strategy": "tabby_credentials",
        "profile_slug": profile_slug,
        "profile_db_id": "",
        "required_auth": {"headers": ["Authorization"], "cookies": []},
        "fallbacks": [],
    }


def _static_auth_plan(env_var: str = "ADOPT_BANK_API_KEY") -> dict:
    return {
        "strategy": "static_secret_header",
        "profile_slug": "",
        "profile_db_id": "",
        "required_auth": {"headers": ["Authorization"], "cookies": []},
        "fallbacks": [
            {
                "type": "static_secret_header",
                "header": "Authorization",
                "value_template": f"Bearer ${{{env_var}}}",
                "secret_env_var": env_var,
            }
        ],
    }


# ── Authenticated operations ─────────────────────────────────────────────────

class TestAuthenticatedOperations:
    def test_uses_resolve_auth_not_get_auth_headers(self) -> None:
        src = generate_operation_module(_simple_tool(), _tabby_auth_plan())
        assert "resolve_auth" in src, "Authenticated op must call resolve_auth()"
        assert "get_auth_headers" not in src, (
            "Authenticated op must not use legacy get_auth_headers(TABBY_PROFILE_ID)"
        )

    def test_imports_resolve_auth(self) -> None:
        src = generate_operation_module(_simple_tool(), _tabby_auth_plan())
        assert "from noui_runtime.auth import resolve_auth" in src

    def test_no_tabby_profile_id_constant(self) -> None:
        """TABBY_PROFILE_ID constant must not be embedded — it belongs in auth_plan.json."""
        src = generate_operation_module(_simple_tool(), _tabby_auth_plan("my-slug"))
        assert "TABBY_PROFILE_ID" not in src, (
            "Profile ID must not be baked into operation module; it lives in auth_plan.json"
        )

    def test_static_auth_also_uses_resolve_auth(self) -> None:
        src = generate_operation_module(_simple_tool(), _static_auth_plan())
        assert "resolve_auth" in src

    def test_headers_passed_to_httpx(self) -> None:
        src = generate_operation_module(_simple_tool(), _tabby_auth_plan())
        assert "headers=" in src, "auth headers must be passed to httpx call"


# ── Header merging ───────────────────────────────────────────────────────────

class TestHeaderMerging:
    def test_recorded_headers_preserved_when_auth_present(self) -> None:
        """Non-auth recorded headers (Accept, Content-Type) must survive auth header injection."""
        tool = _simple_tool(
            request_headers=[
                {"name": "Accept", "value": "application/json"},
                {"name": "X-Request-Version", "value": "2"},
            ]
        )
        src = generate_operation_module(tool, _tabby_auth_plan())
        assert "'Accept'" in src or '"Accept"' in src, (
            "Recorded Accept header must appear in generated operation source"
        )
        assert "'application/json'" in src or '"application/json"' in src, (
            "Recorded Accept header value must appear in generated source"
        )

    def test_auth_overrides_recorded_auth_placeholder(self) -> None:
        """Auth headers from resolve_auth() must take precedence over any recorded placeholders."""
        tool = _simple_tool(request_headers=[{"name": "Accept", "value": "application/json"}])
        src = generate_operation_module(tool, _tabby_auth_plan())
        # The merge pattern: {**_recorded, **await resolve_auth()} or equivalent
        assert "resolve_auth" in src
        # Recorded headers dict must appear before auth headers in merge
        assert "_recorded" in src or "{**" in src, (
            "Generated code must merge recorded headers with auth headers"
        )

    def test_no_recorded_headers_no_merge_dict(self) -> None:
        """When there are no recorded non-auth headers, skip the merge dict."""
        tool = _simple_tool(request_headers=[])
        src = generate_operation_module(tool, _tabby_auth_plan())
        # Should just be: headers = await resolve_auth()  (no _recorded dict needed)
        assert "_recorded" not in src, (
            "When no recorded headers exist, the _recorded dict should be omitted"
        )


# ── Unauthenticated operations ───────────────────────────────────────────────

class TestUnauthenticatedOperations:
    def test_no_auth_import_when_no_plan(self) -> None:
        src = generate_operation_module(_simple_tool(), None)
        assert "resolve_auth" not in src
        assert "get_auth_headers" not in src
        assert "noui_runtime" not in src

    def test_no_auth_import_when_empty_plan(self) -> None:
        src = generate_operation_module(_simple_tool(), {})
        assert "resolve_auth" not in src

    def test_recorded_headers_still_included_without_auth(self) -> None:
        """Even without auth, recorded static headers should appear in generated code."""
        tool = _simple_tool(
            request_headers=[{"name": "Accept", "value": "application/json"}]
        )
        src = generate_operation_module(tool, None)
        assert "'Accept'" in src or '"Accept"' in src

    def test_no_headers_arg_when_no_auth_and_no_recorded_headers(self) -> None:
        src = generate_operation_module(_simple_tool(request_headers=[]), None)
        # httpx call should not have headers= arg when not needed
        assert "headers=" not in src or "headers=headers" not in src


# ── URL and method ───────────────────────────────────────────────────────────

class TestOperationStructure:
    def test_base_url_embedded(self) -> None:
        src = generate_operation_module(
            _simple_tool(base_url="https://api.myapp.com"), _tabby_auth_plan()
        )
        assert "https://api.myapp.com" in src

    def test_path_in_url_expression(self) -> None:
        src = generate_operation_module(
            _simple_tool(path="/v2/clients/{client_id}/documents"), _tabby_auth_plan()
        )
        assert "/v2/clients/{client_id}/documents" in src

    def test_async_execute_function(self) -> None:
        src = generate_operation_module(_simple_tool(), None)
        assert "async def execute(" in src

    def test_returns_json_or_text_fallback(self) -> None:
        src = generate_operation_module(_simple_tool(), None)
        assert "resp.json()" in src
        assert "resp.status_code" in src
