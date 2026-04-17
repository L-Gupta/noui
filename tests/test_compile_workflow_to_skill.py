"""Integration tests for compile_workflow_to_skill.

Mirror of test_compile_workflow.py but asserts skill-shaped output:
SKILL.md frontmatter, executable CLI operations, skill manifest schema.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

_NOUI_ROOT = Path(__file__).resolve().parent.parent
if str(_NOUI_ROOT) not in sys.path:
    sys.path.insert(0, str(_NOUI_ROOT))

from compiler.skill.skill_generator import compile_workflow_to_skill


def _har(entries: list[dict]) -> dict:
    return {"log": {"version": "1.2", "entries": entries}}


def _entry(
    url: str,
    method: str = "GET",
    status: int = 200,
    request_headers: list[dict] | None = None,
    response_headers: list[dict] | None = None,
    post_data: dict | None = None,
) -> dict:
    return {
        "request": {
            "method": method,
            "url": url,
            "headers": request_headers or [],
            "queryString": [],
            "postData": post_data or {},
        },
        "response": {
            "status": status,
            "headers": response_headers or [],
            "content": {"mimeType": "application/json", "text": "{}"},
        },
        "time": 100,
    }


def _compile(
    har: dict,
    *,
    app_slug: str = "example-app",
    session_name: str = "Example Workflow",
    profile_slug: str = "",
    description_override: str = "",
) -> tuple[Path, dict]:
    tmp = Path(tempfile.mkdtemp())
    manifest = compile_workflow_to_skill(
        session_id="abcdef12-3456-7890-abcd-ef1234567890",
        session_name=session_name,
        app_slug=app_slug,
        tabby_profile_id=profile_slug,
        profile_slug=profile_slug,
        har=har,
        click_events=[],
        url_events=[],
        output_dir=str(tmp),
        description_override=description_override,
    )
    return tmp, manifest


# ---------------------------------------------------------------------------
# File tree
# ---------------------------------------------------------------------------


class TestSkillOutputTree:
    def test_unauth_minimum_tree(self) -> None:
        out, manifest = _compile(
            _har([_entry("https://api.example.com/v1/widgets?id=1")]),
        )
        assert (out / "SKILL.md").is_file()
        assert (out / "manifest.json").is_file()
        assert (out / "API.md").is_file()
        assert (out / "noui_runtime" / "auth.py").is_file()
        assert (out / "operations" / "__init__.py").is_file()
        # No auth_plan.json in the unauth case
        assert not (out / "auth_plan.json").exists()
        # At least one operation
        op_files = list((out / "operations").glob("*.py"))
        assert any(p.name != "__init__.py" for p in op_files)

    def test_auth_tree_includes_auth_plan(self) -> None:
        har = _har(
            [
                _entry(
                    "https://api.example.com/v1/widgets",
                    method="POST",
                    request_headers=[
                        {"name": "Authorization", "value": "Bearer tok"},
                        {"name": "Content-Type", "value": "application/json"},
                    ],
                    post_data={"mimeType": "application/json", "text": '{"name":"x"}'},
                    status=201,
                ),
            ]
        )
        out, manifest = _compile(har, profile_slug="example")
        assert (out / "auth_plan.json").is_file()
        auth_plan = json.loads((out / "auth_plan.json").read_text())
        assert auth_plan.get("profile_slug") == "example"


# ---------------------------------------------------------------------------
# SKILL.md frontmatter + body
# ---------------------------------------------------------------------------


class TestSkillMd:
    def test_frontmatter_has_name_and_description(self) -> None:
        out, _ = _compile(_har([_entry("https://api.example.com/v1/widgets")]))
        body = (out / "SKILL.md").read_text()
        assert body.startswith("---\n")
        # Split frontmatter
        end = body.index("\n---\n", 4)
        frontmatter = body[4:end]
        assert "name: example-app" in frontmatter
        # Description line present and non-empty
        desc_line = next(
            (line for line in frontmatter.splitlines() if line.startswith("description:")),
            "",
        )
        assert desc_line.strip() != "description:"

    def test_description_override_wins(self) -> None:
        out, _ = _compile(
            _har([_entry("https://api.example.com/v1/widgets")]),
            description_override="Custom hand-written description that overrides the heuristic.",
        )
        body = (out / "SKILL.md").read_text()
        assert "Custom hand-written description that overrides the heuristic." in body

    def test_body_lists_every_operation(self) -> None:
        out, manifest = _compile(
            _har(
                [
                    _entry("https://api.example.com/v1/a"),
                    _entry("https://api.example.com/v1/b"),
                ]
            )
        )
        body = (out / "SKILL.md").read_text()
        for op in manifest["operations"]:
            assert f"### `{op['name']}`" in body

    def test_auth_body_mentions_tabby_session(self) -> None:
        har = _har(
            [
                _entry(
                    "https://api.example.com/widgets",
                    request_headers=[{"name": "Authorization", "value": "Bearer t"}],
                )
            ]
        )
        out, _ = _compile(har, profile_slug="example")
        body = (out / "SKILL.md").read_text()
        assert "tabby session ensure" in body.lower() or "Tabby session" in body


# ---------------------------------------------------------------------------
# Operation scripts are standalone executables
# ---------------------------------------------------------------------------


class TestOperationScripts:
    def test_all_operations_py_compile(self) -> None:
        out, _ = _compile(_har([_entry("https://api.example.com/v1/widgets?id=1")]))
        for py_file in (out / "operations").glob("*.py"):
            r = subprocess.run(
                [sys.executable, "-m", "py_compile", str(py_file)],
                capture_output=True,
            )
            assert r.returncode == 0, f"py_compile failed for {py_file}: {r.stderr.decode()}"
        r = subprocess.run(
            [sys.executable, "-m", "py_compile", str(out / "noui_runtime" / "auth.py")],
            capture_output=True,
        )
        assert r.returncode == 0, r.stderr.decode()

    def test_operation_cli_help(self) -> None:
        out, manifest = _compile(_har([_entry("https://api.example.com/v1/widgets?id=1")]))
        op_name = manifest["operations"][0]["name"]
        op_file = out / "operations" / f"{op_name}.py"
        r = subprocess.run([sys.executable, str(op_file), "--help"], capture_output=True)
        assert r.returncode == 0, r.stderr.decode()
        stdout = r.stdout.decode()
        assert "usage:" in stdout

    def test_operation_has_execute_coroutine(self) -> None:
        out, manifest = _compile(_har([_entry("https://api.example.com/v1/widgets")]))
        op_name = manifest["operations"][0]["name"]
        src = (out / "operations" / f"{op_name}.py").read_text()
        assert "async def execute(" in src
        assert 'if __name__ == "__main__":' in src


# ---------------------------------------------------------------------------
# Manifest schema
# ---------------------------------------------------------------------------


class TestSkillManifest:
    def test_manifest_runtime_type(self) -> None:
        _, manifest = _compile(_har([_entry("https://api.example.com/v1/widgets")]))
        assert manifest["runtime"]["type"] == "claude-code-skill"
        assert manifest["runtime"]["entrypoint"] == "SKILL.md"
        assert manifest["runtime"]["operation_style"] == "subprocess-cli"

    def test_manifest_skill_id_equals_app_slug(self) -> None:
        _, manifest = _compile(
            _har([_entry("https://api.example.com/v1/widgets")]), app_slug="my-app"
        )
        assert manifest["skill_id"] == "my-app"

    def test_manifest_operations_shape(self) -> None:
        _, manifest = _compile(
            _har(
                [
                    _entry(
                        "https://api.example.com/v1/widgets?id=1",
                        method="GET",
                    )
                ]
            )
        )
        ops = manifest["operations"]
        assert ops, "manifest.operations should not be empty"
        op = ops[0]
        assert "name" in op
        assert "module" in op
        assert op["entry"] == "execute"
        assert "args" in op

    def test_manifest_auth_fields_when_authed(self) -> None:
        har = _har(
            [
                _entry(
                    "https://api.example.com/v1/widgets",
                    request_headers=[{"name": "Authorization", "value": "Bearer t"}],
                )
            ]
        )
        _, manifest = _compile(har, profile_slug="example")
        assert manifest["auth"]["requires_auth"] is True
        assert manifest["auth"]["profile_slug"] == "example"
        assert manifest["auth"]["auth_plan_file"] == "auth_plan.json"


# ---------------------------------------------------------------------------
# Shared runtime — identical bytes for MCP and Skill outputs
# ---------------------------------------------------------------------------


class TestSharedRuntime:
    def test_auth_py_matches_mcp_output(self) -> None:
        """The noui_runtime/auth.py generated for a Skill must be byte-identical to
        the one generated for the equivalent MCP server — that's the whole point
        of compiler/runtime/."""
        from compiler.mcp.server_generator import compile_workflow

        har = _har([_entry("https://api.example.com/v1/widgets")])
        skill_out = Path(tempfile.mkdtemp())
        mcp_out = Path(tempfile.mkdtemp())
        compile_workflow_to_skill(
            session_id="abcdef12-3456-7890-abcd-ef1234567890",
            session_name="Example",
            app_slug="example",
            tabby_profile_id="",
            har=har,
            click_events=[],
            url_events=[],
            output_dir=str(skill_out),
        )
        compile_workflow(
            session_id="abcdef12-3456-7890-abcd-ef1234567890",
            session_name="Example",
            app_slug="example",
            tabby_profile_id="",
            har=har,
            click_events=[],
            url_events=[],
            output_dir=str(mcp_out),
        )
        skill_auth = (skill_out / "noui_runtime" / "auth.py").read_bytes()
        mcp_auth = (mcp_out / "noui_runtime" / "auth.py").read_bytes()
        assert skill_auth == mcp_auth
