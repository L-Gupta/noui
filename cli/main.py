#!/usr/bin/env python3
"""
NoUI developer CLI — backend lifecycle, login recording, workflow recording, and MCP servers.

Subcommands:
    start                              - Start the NoUI backend
    stop                               - Stop the NoUI backend
    status                             - Show backend + Tabby status

    login record <app> <url>           - Create login session + print extension instructions
    login list                         - List login sessions
    login export <session_id>          - Analyze session → write bundle JSON
    login review <bundle_file>         - Print review items from bundle file
    login register <bundle_file>       - Register with Tabby (needs TABBY_ADMIN_TOKEN)
    login validate <bundle_file>       - Wait for Tabby profile to become HEALTHY
    login import <session_id>          - export + review + register [+ validate]

    workflow record <name> <url>       - Create workflow session + print extension instructions
    workflow list                      - List workflow sessions
    workflow captures                  - List capture sessions recorded via the extension
    workflow export-mcp <session_id>   - Compile workflow to FastMCP server

    mcp list                           - List generated MCP servers
    mcp status <server_id>             - Show status of a generated MCP server
    mcp start <server_id>              - Start a generated MCP server
    mcp stop <server_id>               - Stop a generated MCP server
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CLI_DIR = Path(__file__).parent
NOUI_DIR = CLI_DIR.parent
MCP_SERVERS_DIR = NOUI_DIR / "mcp_servers"
LOGIN_RECORDINGS_DIR = NOUI_DIR / "login_recordings"
NOUI_PID_FILE = NOUI_DIR / ".noui-backend.pid"
NOUI_LOG_FILE = NOUI_DIR / ".noui-backend.log"

NOUI_PORT = int(os.environ.get("NOUI_PORT", "8002"))
BACKEND_URL = f"http://localhost:{NOUI_PORT}"

TABBY_DIR = NOUI_DIR.parent / "tabby"
TABBY_API_HOST = os.environ.get("TABBY_API_HOST", "http://localhost:8080")
ENV_LOCAL = TABBY_DIR / ".env.local"

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

try:
    from colorama import Fore, Style
    from colorama import init as _colorama_init

    _colorama_init()

    def _green(s: str) -> str:
        return f"{Fore.GREEN}{s}{Style.RESET_ALL}"

    def _red(s: str) -> str:
        return f"{Fore.RED}{s}{Style.RESET_ALL}"

    def _yellow(s: str) -> str:
        return f"{Fore.YELLOW}{s}{Style.RESET_ALL}"

    def _cyan(s: str) -> str:
        return f"{Fore.CYAN}{s}{Style.RESET_ALL}"

    def _bold(s: str) -> str:
        return f"{Style.BRIGHT}{s}{Style.RESET_ALL}"

except ImportError:

    def _green(s: str) -> str:
        return s

    def _red(s: str) -> str:
        return s

    def _yellow(s: str) -> str:
        return s

    def _cyan(s: str) -> str:
        return s

    def _bold(s: str) -> str:
        return s


# ---------------------------------------------------------------------------
# PID helpers
# ---------------------------------------------------------------------------


def _read_pid(pid_file: Path) -> int | None:
    if pid_file.exists():
        try:
            return int(pid_file.read_text().strip())
        except Exception:
            pass
    return None


def _clear_pid(pid_file: Path) -> None:
    if pid_file.exists():
        pid_file.unlink()


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ---------------------------------------------------------------------------
# Backend HTTP helpers
# ---------------------------------------------------------------------------


def _http(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any] | list[Any]:
    url = BACKEND_URL + path
    data = json.dumps(body).encode() if body is not None else b""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {method} {path}: {body_text}") from exc


def _backend_alive() -> bool:
    try:
        with urllib.request.urlopen(BACKEND_URL + "/health", timeout=3) as resp:
            return json.loads(resp.read().decode()).get("status") == "ok"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Tabby HTTP helpers
# ---------------------------------------------------------------------------


def _tabby_http(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: int = 15,
) -> dict[str, Any] | list[Any]:
    url = TABBY_API_HOST + path
    data = json.dumps(body).encode() if body is not None else b""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {method} {path}: {body_text}") from exc


def _tabby_alive() -> bool:
    try:
        with urllib.request.urlopen(TABBY_API_HOST + "/health/live", timeout=3) as resp:
            return json.loads(resp.read().decode()).get("status") == "ok"
    except Exception:
        return False


def _load_env_local() -> dict[str, str]:
    result: dict[str, str] = {}
    if not ENV_LOCAL.exists():
        return result
    for raw_line in ENV_LOCAL.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        result[key.strip()] = val.strip()
    return result


def _get_admin_token() -> str | None:
    # First try env var
    token = os.environ.get("TABBY_ADMIN_TOKEN", "")
    if token:
        return token

    # Fall back to reading credentials from .env.local and logging in
    env_local = _load_env_local()
    email = env_local.get("ADMIN_BOOTSTRAP_EMAIL", "")
    password = env_local.get("ADMIN_BOOTSTRAP_PASSWORD", "")
    if not email or not password:
        print(
            _red(
                f"Set TABBY_ADMIN_TOKEN env var, or set ADMIN_BOOTSTRAP_EMAIL / "
                f"ADMIN_BOOTSTRAP_PASSWORD in {ENV_LOCAL}"
            )
        )
        return None
    try:
        resp = _tabby_http("POST", "/login", {"email": email, "password": password})
        assert isinstance(resp, dict)
        return resp.get("token") or resp.get("access_token", "")
    except (RuntimeError, AssertionError) as exc:
        print(_red(f"Admin login failed: {exc}"))
        return None


# ---------------------------------------------------------------------------
# start / stop / status
# ---------------------------------------------------------------------------


def cmd_start(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Start the NoUI backend."""
    if _backend_alive():
        pid = _read_pid(NOUI_PID_FILE)
        pid_label = f" (PID {pid})" if pid else ""
        print(_yellow(f"NoUI backend is already running{pid_label} at {BACKEND_URL}"))
        return 0

    # Prefer the venv uvicorn, fall back to whichever uvicorn is on PATH
    venv_python = NOUI_DIR / ".venv" / "bin" / "python"
    venv_uvicorn = NOUI_DIR / ".venv" / "bin" / "uvicorn"

    if venv_uvicorn.exists():
        uvicorn_cmd = str(venv_uvicorn)
    else:
        # Try system uvicorn
        import shutil

        uvicorn_cmd = shutil.which("uvicorn") or ""
        if not uvicorn_cmd:
            print(_red("uvicorn not found."))
            if venv_python.exists():
                print(
                    f"  Install deps: {venv_python} -m pip install -r "
                    f"{NOUI_DIR}/backend/requirements.txt"
                )
            else:
                print("  Create a venv and install requirements first.")
            return 1

    NOUI_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(NOUI_LOG_FILE, "a")  # noqa: SIM115
    proc = subprocess.Popen(
        [
            uvicorn_cmd,
            "backend.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(NOUI_PORT),
            "--log-level",
            "info",
        ],
        cwd=str(NOUI_DIR),
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
    )
    NOUI_PID_FILE.write_text(str(proc.pid))
    print(
        f"Starting NoUI backend (PID {proc.pid}) … logs → {_cyan(str(NOUI_LOG_FILE))}",
        end="",
        flush=True,
    )

    for _ in range(30):
        time.sleep(1)
        print(".", end="", flush=True)
        if _backend_alive():
            break
    else:
        print()
        print(_red(f"Backend did not become ready within 30s. Check logs: {NOUI_LOG_FILE}"))
        _clear_pid(NOUI_PID_FILE)
        return 1

    print()
    print(_green(f"NoUI backend ready at {BACKEND_URL}"))
    print()
    print("  Next steps:")
    print(f"    {_bold('noui login record <app> <url>')}    — start a login recording")
    print(f"    {_bold('noui workflow record <name> <url>')} — start a workflow recording")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Stop the NoUI backend."""
    pid = _read_pid(NOUI_PID_FILE)
    if pid is None:
        if _backend_alive():
            print(_yellow("Backend is running but PID file not found — stop it manually."))
            return 1
        print(_yellow("NoUI backend is not running."))
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        _clear_pid(NOUI_PID_FILE)
        print(_green(f"NoUI backend (PID {pid}) stopped."))
        return 0
    except ProcessLookupError:
        _clear_pid(NOUI_PID_FILE)
        print(_yellow(f"Process {pid} was not running — cleared stale PID file."))
        return 0
    except Exception as exc:
        print(_red(f"Failed to stop backend: {exc}"))
        return 1


def cmd_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Show backend, Tabby, and session counts."""
    pid = _read_pid(NOUI_PID_FILE)
    alive = _backend_alive()

    print(_bold("NoUI backend:"))
    if alive:
        pid_label = f" (PID {pid})" if pid else ""
        print(_green(f"  Running at {BACKEND_URL}{pid_label}"))
    else:
        print(_red(f"  Not reachable at {BACKEND_URL}"))
        if pid:
            print(_yellow(f"     (stale PID file: {pid})"))
        print(f"     Run: {_bold('noui start')}")

    print()
    print(_bold("Tabby API:"))
    if _tabby_alive():
        print(_green(f"  Reachable at {TABBY_API_HOST}"))
    else:
        print(_yellow(f"  Not reachable at {TABBY_API_HOST}"))

    if alive:
        print()
        print(_bold("Sessions:"))
        try:
            login_sessions = _http("GET", "/login-sessions")
            assert isinstance(login_sessions, list)
            print(f"  Login sessions    : {len(login_sessions)}")
        except Exception:
            print(f"  Login sessions    : {_yellow('(could not fetch)')}")
        try:
            wf_sessions = _http("GET", "/workflow-sessions")
            assert isinstance(wf_sessions, list)
            print(f"  Workflow sessions : {len(wf_sessions)}")
        except Exception:
            print(f"  Workflow sessions : {_yellow('(could not fetch)')}")

    return 0 if alive else 1


# ---------------------------------------------------------------------------
# login subcommands
# ---------------------------------------------------------------------------


def cmd_login_record(args: argparse.Namespace) -> int:
    """Create a login session and print extension instructions."""
    if not _backend_alive():
        print(_red(f"NoUI backend not reachable at {BACKEND_URL}"))
        print(f"  Start it with: {_bold('noui start')}")
        return 1

    app_name: str = args.app
    login_url: str = args.url

    print(f"Creating login session for {_cyan(app_name)} …", end=" ", flush=True)
    try:
        resp = _http("POST", "/login-sessions", {"app_name": app_name, "login_url": login_url})
        assert isinstance(resp, dict)
    except (RuntimeError, AssertionError) as exc:
        print()
        print(_red(f"Failed to create login session: {exc}"))
        return 1

    session_id: str = resp.get("id", "")
    print(_green("done"))
    print()
    print(_bold("Login session created:"))
    print(f"  Session ID : {_cyan(session_id)}")
    print(f"  App        : {app_name}")
    print(f"  Login URL  : {login_url}")
    print()
    print(_bold("Next steps:"))
    print("  1. Open Chrome and load the NoUI extension")
    print("  2. Select 'Login Recording' mode in the extension")
    print(f"  3. Navigate to {login_url} and record your login")
    print("  4. When done, click Complete in the extension")
    print()
    print(f"  Then run: {_bold(f'noui login export {session_id}')}")
    return 0


def cmd_login_list(args: argparse.Namespace) -> int:  # noqa: ARG001
    """List login sessions."""
    if not _backend_alive():
        print(_red(f"NoUI backend not reachable at {BACKEND_URL}"))
        return 1

    try:
        sessions = _http("GET", "/login-sessions")
        assert isinstance(sessions, list)
    except (RuntimeError, AssertionError) as exc:
        print(_red(f"Failed to list sessions: {exc}"))
        return 1

    if not sessions:
        print(_yellow("No login sessions found."))
        return 0

    print(_bold("Login sessions:"))
    print()
    for s in sessions:
        sid = s.get("id", "?")
        app = s.get("app_name") or "?"
        url = s.get("login_url") or "?"
        status = s.get("status") or "?"
        if status == "completed":
            color = _green
        elif status in ("recording", "capturing"):
            color = _yellow
        else:
            color = str
        print(f"  {_cyan(sid[:8])}  {_bold(app)}  {color(status)}  {url}")

    print()
    return 0


def cmd_login_export(args: argparse.Namespace) -> int:
    """Analyze session and write bundle JSON to login_recordings/."""
    if not _backend_alive():
        print(_red(f"NoUI backend not reachable at {BACKEND_URL}"))
        return 1

    session_id: str = args.session_id

    print(f"Analyzing session {_cyan(session_id)} …", end=" ", flush=True)
    try:
        bundle = _http("POST", f"/login-sessions/{session_id}/analyze")
        assert isinstance(bundle, dict)
        print(_green("done"))
    except (RuntimeError, AssertionError) as exc:
        print()
        print(_red(f"Analysis failed: {exc}"))
        return 1

    LOGIN_RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    bundle_path = LOGIN_RECORDINGS_DIR / f"noui-{session_id[:8]}-bundle.json"
    bundle_path.write_text(json.dumps(bundle, indent=2) + "\n")

    review_items = bundle.get("review_items", [])
    errors = [r for r in review_items if r.get("severity") == "error"]
    warnings = [r for r in review_items if r.get("severity") == "warning"]

    print()
    print(_bold("Bundle written:"))
    print(f"  {bundle_path}")
    print()
    if errors:
        print(_red(f"  {len(errors)} error(s) — fix before registering"))
    if warnings:
        print(_yellow(f"  {len(warnings)} warning(s) — review selector confidence"))
    if not errors and not warnings:
        print(_green("  No issues — ready to register"))
    print()
    print(f"  Run: {_bold(f'noui login review {bundle_path}')}")
    return 0


def cmd_login_review(args: argparse.Namespace) -> int:
    """Print review items from a bundle file."""
    bundle_path = Path(args.bundle_file)
    if not bundle_path.exists():
        print(_red(f"Bundle file not found: {bundle_path}"))
        return 1

    try:
        bundle = json.loads(bundle_path.read_text())
    except Exception as exc:
        print(_red(f"Failed to parse bundle: {exc}"))
        return 1

    review_items = bundle.get("review_items", [])
    validation = bundle.get("validation", {})
    recording = bundle.get("recording", {})

    print(_bold("Review Report"))
    print(f"  Session  : {recording.get('session_id', '?')}")
    gen_valid = validation.get("generator_valid", True)
    print(f"  Generator valid: {'Yes' if gen_valid else _red('No')}")
    print()

    if validation.get("issues"):
        print(_bold("Generator issues:"))
        for issue in validation["issues"]:
            print(f"  {_red('x')} {issue}")
        print()

    errors = [r for r in review_items if r.get("severity") == "error"]
    warnings = [r for r in review_items if r.get("severity") == "warning"]
    infos = [r for r in review_items if r.get("severity") == "info"]

    if errors:
        print(_bold("Errors:"))
        for r in errors:
            print(f"  {_red('x')} [{r.get('type')}] {r.get('message')}")
        print()
    if warnings:
        print(_bold("Warnings:"))
        for r in warnings:
            print(f"  {_yellow('!')} [{r.get('type')}] {r.get('message')}")
        print()
    if infos:
        print(_bold("Info:"))
        for r in infos:
            print(f"  {_cyan('i')} [{r.get('type')}] {r.get('message')}")
        print()

    app_draft = bundle.get("application_draft", {})
    steps = app_draft.get("login_config", {}).get("steps", [])
    if steps:
        print(_bold("Generated login steps:"))
        for i, step in enumerate(steps, 1):
            action = step.get("action", "?")
            sel = step.get("selector", "")
            val = step.get("value", "")
            url = step.get("url", "")
            sensitive = " [sensitive]" if step.get("sensitive") else ""
            if action == "goto":
                print(f"  {i}. goto {url}")
            elif action == "fill":
                print(f"  {i}. fill {sel!r} = {val!r}{sensitive}")
            elif action == "click":
                print(f"  {i}. click {sel!r}")
            elif action in ("wait_for", "wait_for_url"):
                target = sel or step.get("url_pattern", "")
                print(f"  {i}. {action} {target!r}{sensitive}")
            else:
                print(f"  {i}. {action} {(sel or url)!r}")
        print()

    if not errors and not warnings:
        print(_green("No issues — ready to register"))
    return 0


def cmd_login_register(args: argparse.Namespace) -> int:
    """Register bundle with Tabby: create Application + STAGING ServiceProfile."""
    if not _tabby_alive():
        print(_red(f"Tabby API not reachable at {TABBY_API_HOST}"))
        return 1

    bundle_path = Path(args.bundle_file)
    if not bundle_path.exists():
        print(_red(f"Bundle file not found: {bundle_path}"))
        return 1

    try:
        bundle = json.loads(bundle_path.read_text())
    except Exception as exc:
        print(_red(f"Failed to parse bundle: {exc}"))
        return 1

    validation = bundle.get("validation", {})
    if not validation.get("generator_valid", True):
        print(_red("Bundle has generator errors — fix them before registering"))
        print(f"  Run: {_bold(f'noui login review {bundle_path}')}")
        return 1

    app_draft = bundle.get("application_draft", {})
    profile_draft = bundle.get("service_profile_draft", {})
    profile_id = profile_draft.get("profile_id", "")

    if not profile_id:
        print(_red("Bundle is missing service_profile_draft.profile_id"))
        return 1

    admin_token = _get_admin_token()
    if not admin_token:
        return 1

    # Create Application
    print(f"Creating Application '{profile_id}' …", end=" ", flush=True)
    try:
        patched_draft = dict(app_draft)
        patched_urls = [
            u.replace("http://localhost", "https://localhost", 1)
            if u.startswith("http://localhost")
            else u
            for u in (app_draft.get("target_urls") or [])
        ]
        if patched_urls:
            patched_draft = {**app_draft, "target_urls": patched_urls}
        app_resp = _tabby_http("POST", "/apps", patched_draft, token=admin_token)
        assert isinstance(app_resp, dict)
        app_id: str = app_resp["app_id"]
        print(_green("done"))
    except (RuntimeError, KeyError, AssertionError) as exc:
        print()
        print(_red(f"Application creation failed: {exc}"))
        return 1

    # Create STAGING ServiceProfile
    profile_payload = {**profile_draft, "app_id": app_id}
    t = time.localtime()
    profile_payload["version"] = f"{t.tm_year % 100}.{t.tm_mon}.{t.tm_mday}"

    print(f"Creating STAGING ServiceProfile '{profile_id}' …", end=" ", flush=True)
    try:
        prof_resp = _tabby_http("POST", "/admin/profiles", profile_payload, token=admin_token)
        assert isinstance(prof_resp, dict)
        profile_db_id: str = prof_resp["id"]
        print(_green("done"))
    except (RuntimeError, KeyError, AssertionError) as exc:
        print()
        print(_red(f"ServiceProfile creation failed: {exc}"))
        return 1

    # Update bundle file with registered IDs
    bundle["_provisioned"] = {
        "app_id": app_id,
        "profile_db_id": profile_db_id,
        "profile_id": profile_id,
        "version_state": "STAGING",
    }
    bundle_path.write_text(json.dumps(bundle, indent=2) + "\n")

    print()
    print(_green(f"Registered profile '{profile_id}'"))
    print(f"  Application ID       : {_cyan(app_id)}")
    print(f"  ServiceProfile DB ID : {_cyan(profile_db_id)}")
    print(f"  Tabby profile ID     : {_cyan(profile_db_id)}")
    print("  Version state        : STAGING")
    print()
    print("  Next steps:")
    print(f"    {_bold(f'noui login validate {bundle_path}')}")
    return 0


def cmd_login_validate(args: argparse.Namespace) -> int:
    """Wait for Tabby profile to become HEALTHY."""
    if not _tabby_alive():
        print(_red(f"Tabby API not reachable at {TABBY_API_HOST}"))
        return 1

    bundle_path = Path(args.bundle_file)
    if not bundle_path.exists():
        print(_red(f"Bundle file not found: {bundle_path}"))
        return 1

    try:
        bundle = json.loads(bundle_path.read_text())
    except Exception as exc:
        print(_red(f"Failed to parse bundle: {exc}"))
        return 1

    provisioned = bundle.get("_provisioned", {})
    profile_db_id = provisioned.get("profile_db_id", "")
    if not profile_db_id:
        print(_red("No profile_db_id found in bundle — run `noui login register` first"))
        return 1

    admin_token = _get_admin_token()
    if not admin_token:
        return 1

    print(f"Polling profile {_cyan(profile_db_id)} for HEALTHY (up to 60s)", end="", flush=True)
    deadline = time.time() + 60
    final_state = ""
    while time.time() < deadline:
        time.sleep(3)
        print(".", end="", flush=True)
        try:
            resp = _tabby_http(
                "GET", f"/admin/service-profiles/{profile_db_id}", token=admin_token
            )
            assert isinstance(resp, dict)
            final_state = resp.get("state", resp.get("status", ""))
            if final_state in ("HEALTHY", "ACTIVE"):
                break
            if final_state in ("FAILED", "ERROR"):
                print()
                print(_red(f"Profile entered failed state: {final_state}"))
                return 1
        except (RuntimeError, AssertionError):
            pass
    else:
        print()
        print(_red("Profile did not reach HEALTHY within 60s"))
        return 1

    print()
    print(_green(f"Profile '{profile_db_id}' is {final_state}"))
    return 0


def cmd_login_import(args: argparse.Namespace) -> int:
    """Convenience wrapper: export + review + register [+ validate]."""
    session_id: str = args.session_id

    # export
    export_args = argparse.Namespace(session_id=session_id)
    rc = cmd_login_export(export_args)
    if rc != 0:
        return rc

    bundle_path = LOGIN_RECORDINGS_DIR / f"noui-{session_id[:8]}-bundle.json"

    # review
    review_args = argparse.Namespace(bundle_file=str(bundle_path))
    cmd_login_review(review_args)

    # register
    register_args = argparse.Namespace(bundle_file=str(bundle_path))
    rc = cmd_login_register(register_args)
    if rc != 0:
        return rc

    # optionally validate
    if getattr(args, "validate", False):
        validate_args = argparse.Namespace(bundle_file=str(bundle_path))
        rc = cmd_login_validate(validate_args)
        if rc != 0:
            return rc

    return 0


# ---------------------------------------------------------------------------
# workflow subcommands
# ---------------------------------------------------------------------------


def cmd_workflow_record(args: argparse.Namespace) -> int:
    """Create a workflow session and print extension instructions."""
    if not _backend_alive():
        print(_red(f"NoUI backend not reachable at {BACKEND_URL}"))
        print(f"  Start it with: {_bold('noui start')}")
        return 1

    name: str = args.name
    start_url: str = args.url

    print(f"Creating workflow session '{_cyan(name)}' …", end=" ", flush=True)
    try:
        resp = _http(
            "POST",
            "/workflow-sessions",
            {"name": name, "start_url": start_url, "description": ""},
        )
        assert isinstance(resp, dict)
    except (RuntimeError, AssertionError) as exc:
        print()
        print(_red(f"Failed to create workflow session: {exc}"))
        return 1

    session_id: str = resp.get("id", "")
    print(_green("done"))
    print()
    print(_bold("Workflow session created:"))
    print(f"  Session ID : {_cyan(session_id)}")
    print(f"  Name       : {name}")
    print(f"  Start URL  : {start_url}")
    print()
    print(_bold("Next steps:"))
    print("  1. Open Chrome and load the NoUI extension")
    print("  2. Select 'Workflow Recording' mode in the extension")
    print(f"  3. Navigate to {start_url} and perform your workflow")
    print("  4. When done, click Complete in the extension")
    print()
    print(f"  Then run: {_bold(f'noui workflow export-mcp {session_id} --profile <tabby_profile_id>')}")
    return 0


def cmd_workflow_list(args: argparse.Namespace) -> int:  # noqa: ARG001
    """List workflow sessions."""
    if not _backend_alive():
        print(_red(f"NoUI backend not reachable at {BACKEND_URL}"))
        return 1

    try:
        sessions = _http("GET", "/workflow-sessions")
        assert isinstance(sessions, list)
    except (RuntimeError, AssertionError) as exc:
        print(_red(f"Failed to list sessions: {exc}"))
        return 1

    if not sessions:
        print(_yellow("No workflow sessions found."))
        return 0

    print(_bold("Workflow sessions:"))
    print()
    for s in sessions:
        sid = s.get("id", "?")
        name = s.get("name") or "?"
        url = s.get("start_url") or "?"
        status = s.get("status") or "?"
        if status == "completed":
            color = _green
        elif status in ("recording", "capturing"):
            color = _yellow
        else:
            color = str
        print(f"  {_cyan(sid[:8])}  {_bold(name)}  {color(status)}  {url}")

    print()
    return 0


def cmd_workflow_captures(args: argparse.Namespace) -> int:  # noqa: ARG001
    """List capture sessions (recorded via the extension)."""
    if not _backend_alive():
        print(_red(f"NoUI backend not reachable at {BACKEND_URL}"))
        return 1

    try:
        sessions = _http("GET", "/capture-sessions")
        assert isinstance(sessions, list)
    except (RuntimeError, AssertionError) as exc:
        print(_red(f"Failed to list capture sessions: {exc}"))
        return 1

    if not sessions:
        print(_yellow("No capture sessions found."))
        return 0

    print(_bold("Capture sessions:"))
    print()
    for s in sessions:
        sid = s.get("id", "?")
        status = s.get("status") or "?"
        project_id = s.get("project_id") or ""
        if status == "stopped":
            color = _green
        elif status in ("capturing", "recording"):
            color = _yellow
        else:
            color = str
        print(f"  {_cyan(sid[:8])}  {_cyan(sid)}  {color(status)}  project:{project_id[:8] if project_id else '—'}")

    print()
    return 0


def cmd_workflow_export_mcp(args: argparse.Namespace) -> int:
    """Compile workflow session to a FastMCP server."""
    if not _backend_alive():
        print(_red(f"NoUI backend not reachable at {BACKEND_URL}"))
        return 1

    session_id: str = args.session_id
    profile_id: str = args.profile
    capture_session_id: str = getattr(args, "capture_session", "")

    params = []
    if profile_id:
        params.append(f"tabby_profile_id={profile_id}")
    if capture_session_id:
        params.append(f"capture_session_id={capture_session_id}")
    path = f"/workflow-sessions/{session_id}/export-mcp"
    if params:
        path += "?" + "&".join(params)

    print(f"Exporting workflow {_cyan(session_id)} to MCP …", end=" ", flush=True)
    try:
        result = _http("POST", path)
        assert isinstance(result, dict)
        print(_green("done"))
    except (RuntimeError, AssertionError) as exc:
        print()
        print(_red(f"Export failed: {exc}"))
        return 1

    server_id = result.get("server_id", "?")
    tool_count = result.get("tool_count", len(result.get("tools", [])))
    output_path = result.get("output_path", "?")

    print()
    print(_bold("MCP server generated:"))
    print(f"  Server ID  : {_cyan(server_id)}")
    print(f"  Tools      : {tool_count}")
    print(f"  Output     : {output_path}")
    print()
    print(f"  Run: {_bold(f'noui mcp start {server_id}')}")
    return 0


# ---------------------------------------------------------------------------
# mcp subcommands
# ---------------------------------------------------------------------------


def _find_mcp_manifest(server_id: str) -> Path | None:
    """Search mcp_servers/ for a manifest.json matching server_id."""
    if not MCP_SERVERS_DIR.exists():
        return None
    # Direct path: mcp_servers/<server_id>/manifest.json
    direct = MCP_SERVERS_DIR / server_id / "manifest.json"
    if direct.exists():
        return direct
    # Scan all manifest.json files for matching server_id field
    for manifest_path in MCP_SERVERS_DIR.rglob("manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text())
            if manifest.get("server_id") == server_id:
                return manifest_path
        except Exception:
            continue
    return None


def _mcp_pid_file(manifest_path: Path, server_id: str) -> Path:
    return manifest_path.parent / f".mcp-{server_id}.pid"


def cmd_mcp_list(args: argparse.Namespace) -> int:  # noqa: ARG001
    """List generated MCP servers."""
    if not MCP_SERVERS_DIR.exists():
        print(_yellow("No MCP servers found (mcp_servers/ does not exist)."))
        return 0

    manifests = list(MCP_SERVERS_DIR.rglob("manifest.json"))
    if not manifests:
        print(_yellow("No MCP server manifests found in mcp_servers/."))
        return 0

    print(_bold("MCP servers:"))
    print()
    header = f"  {'SERVER ID':<32} {'APP':<20} {'TOOLS':>5}  STATUS"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for manifest_path in sorted(manifests):
        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:
            continue

        server_id = manifest.get("server_id", "?")
        _app = manifest.get("app", {})
        app = _app.get("name", "?") if isinstance(_app, dict) else str(_app)
        tools = manifest.get("tools", [])
        tool_count = len(tools) if isinstance(tools, list) else manifest.get("tool_count", 0)

        pid_file = _mcp_pid_file(manifest_path, server_id)
        pid = _read_pid(pid_file)
        if pid and _pid_running(pid):
            status = _green(f"running (PID {pid})")
        else:
            status = _red("stopped")
            if pid:
                _clear_pid(pid_file)

        print(f"  {_cyan(server_id):<32} {app:<20} {tool_count:>5}  {status}")

    print()
    return 0


def cmd_mcp_status(args: argparse.Namespace) -> int:
    """Show status of a generated MCP server."""
    server_id: str = args.server_id
    manifest_path = _find_mcp_manifest(server_id)

    if manifest_path is None:
        print(_red(f"MCP server '{server_id}' not found in {MCP_SERVERS_DIR}"))
        return 1

    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as exc:
        print(_red(f"Failed to parse manifest: {exc}"))
        return 1

    _app = manifest.get("app", {})
    app = _app.get("name", "?") if isinstance(_app, dict) else str(_app)
    tools = manifest.get("tools", [])
    tool_count = len(tools) if isinstance(tools, list) else manifest.get("tool_count", 0)

    pid_file = _mcp_pid_file(manifest_path, server_id)
    pid = _read_pid(pid_file)
    running = pid is not None and _pid_running(pid)
    if pid and not running:
        _clear_pid(pid_file)

    print(_bold("MCP server status:"))
    print(f"  Server ID : {_cyan(server_id)}")
    print(f"  App       : {app}")
    print(f"  Tools     : {tool_count}")
    print(f"  Manifest  : {manifest_path}")
    if running:
        print(f"  Status    : {_green(f'running (PID {pid})')}")
    else:
        print(f"  Status    : {_red('stopped')}")
    return 0


def cmd_mcp_start(args: argparse.Namespace) -> int:
    """Start a generated MCP server."""
    server_id: str = args.server_id
    manifest_path = _find_mcp_manifest(server_id)

    if manifest_path is None:
        print(_red(f"MCP server '{server_id}' not found in {MCP_SERVERS_DIR}"))
        return 1

    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as exc:
        print(_red(f"Failed to parse manifest: {exc}"))
        return 1

    pid_file = _mcp_pid_file(manifest_path, server_id)
    existing_pid = _read_pid(pid_file)
    if existing_pid and _pid_running(existing_pid):
        print(_yellow(f"MCP server '{server_id}' is already running (PID {existing_pid})"))
        return 0

    server_dir = manifest_path.parent
    entrypoint = manifest.get("entrypoint", "server.py")
    server_script = server_dir / entrypoint

    if not server_script.exists():
        print(_red(f"Server entrypoint not found: {server_script}"))
        return 1

    # Use noui venv python if available, otherwise system python
    venv_python = NOUI_DIR / ".venv" / "bin" / "python"
    python_cmd = str(venv_python) if venv_python.exists() else sys.executable

    log_path = server_dir / f".mcp-{server_id}.log"
    log_fh = open(log_path, "a")  # noqa: SIM115
    proc = subprocess.Popen(
        [python_cmd, str(server_script)],
        cwd=str(server_dir),
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
    )
    pid_file.write_text(str(proc.pid))
    print(_green(f"MCP server '{server_id}' started (PID {proc.pid})"))
    print(f"  Logs: {log_path}")
    return 0


def cmd_mcp_stop(args: argparse.Namespace) -> int:
    """Stop a generated MCP server."""
    server_id: str = args.server_id
    manifest_path = _find_mcp_manifest(server_id)

    if manifest_path is None:
        print(_red(f"MCP server '{server_id}' not found in {MCP_SERVERS_DIR}"))
        return 1

    pid_file = _mcp_pid_file(manifest_path, server_id)
    pid = _read_pid(pid_file)

    if pid is None:
        print(_yellow(f"MCP server '{server_id}' is not running (no PID file)."))
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        _clear_pid(pid_file)
        print(_green(f"MCP server '{server_id}' (PID {pid}) stopped."))
        return 0
    except ProcessLookupError:
        _clear_pid(pid_file)
        print(_yellow(f"Process {pid} was not running — cleared stale PID file."))
        return 0
    except Exception as exc:
        print(_red(f"Failed to stop MCP server: {exc}"))
        return 1


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noui",
        description="NoUI developer CLI — backend lifecycle, login recording, workflow recording, MCP servers.",
    )
    sub = parser.add_subparsers(dest="command")

    # --- top-level commands ---
    sub.add_parser("start", help=f"Start the NoUI backend (port {NOUI_PORT})")
    sub.add_parser("stop", help="Stop the NoUI backend")
    sub.add_parser("status", help="Show backend + Tabby status and session counts")

    # --- login ---
    login_parser = sub.add_parser("login", help="Login session commands")
    login_sub = login_parser.add_subparsers(dest="login_command")

    login_record = login_sub.add_parser("record", help="Create login session + print instructions")
    login_record.add_argument("app", help="App name (e.g. 'GitHub')")
    login_record.add_argument("url", help="Login page URL")

    login_sub.add_parser("list", help="List login sessions")

    login_export = login_sub.add_parser(
        "export", help="Analyze session and write bundle JSON to login_recordings/"
    )
    login_export.add_argument("session_id", help="Login session ID")

    login_review = login_sub.add_parser("review", help="Print review items from a bundle file")
    login_review.add_argument("bundle_file", help="Path to bundle JSON file")

    login_register = login_sub.add_parser(
        "register", help="Register bundle with Tabby (needs TABBY_ADMIN_TOKEN)"
    )
    login_register.add_argument("bundle_file", help="Path to bundle JSON file")

    login_validate = login_sub.add_parser(
        "validate", help="Wait for Tabby profile to become HEALTHY"
    )
    login_validate.add_argument("bundle_file", help="Path to bundle JSON file")

    login_import = login_sub.add_parser(
        "import", help="Convenience: export + review + register [+ validate]"
    )
    login_import.add_argument("session_id", help="Login session ID")
    login_import.add_argument(
        "--validate", action="store_true", help="Also run validate after register"
    )

    # --- workflow ---
    wf_parser = sub.add_parser("workflow", help="Workflow session commands")
    wf_sub = wf_parser.add_subparsers(dest="workflow_command")

    wf_record = wf_sub.add_parser(
        "record", help="Create workflow session + print extension instructions"
    )
    wf_record.add_argument("name", help="Workflow name")
    wf_record.add_argument("url", help="Start URL")

    wf_sub.add_parser("list", help="List workflow sessions")
    wf_sub.add_parser("captures", help="List capture sessions recorded via the extension")

    wf_export = wf_sub.add_parser("export-mcp", help="Compile workflow session to FastMCP server")
    wf_export.add_argument("session_id", help="Workflow session ID")
    wf_export.add_argument(
        "--profile",
        default="",
        metavar="TABBY_PROFILE_ID",
        help="Tabby profile ID to associate with the MCP server",
    )
    wf_export.add_argument(
        "--capture-session",
        default="",
        metavar="CAPTURE_SESSION_ID",
        help="Use HAR/clicks from an ABCD capture session instead of the workflow session",
    )

    # --- mcp ---
    mcp_parser = sub.add_parser("mcp", help="Generated MCP server commands")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_command")

    mcp_sub.add_parser("list", help="List generated MCP servers")

    mcp_status_p = mcp_sub.add_parser("status", help="Show status of a generated MCP server")
    mcp_status_p.add_argument("server_id", help="MCP server ID")

    mcp_start_p = mcp_sub.add_parser("start", help="Start a generated MCP server")
    mcp_start_p.add_argument("server_id", help="MCP server ID")

    mcp_stop_p = mcp_sub.add_parser("stop", help="Stop a generated MCP server")
    mcp_stop_p.add_argument("server_id", help="MCP server ID")

    return parser


# ---------------------------------------------------------------------------
# Dispatch helpers
# ---------------------------------------------------------------------------


def _dispatch_login(args: argparse.Namespace) -> int:
    cmd = getattr(args, "login_command", None)
    if cmd is None:
        print("Usage: noui login {record,list,export,review,register,validate,import}")
        return 1
    dispatch = {
        "record": cmd_login_record,
        "list": cmd_login_list,
        "export": cmd_login_export,
        "review": cmd_login_review,
        "register": cmd_login_register,
        "validate": cmd_login_validate,
        "import": cmd_login_import,
    }
    fn = dispatch.get(cmd)
    if fn is None:
        print(_red(f"Unknown login subcommand: {cmd}"))
        return 1
    return fn(args)


def _dispatch_workflow(args: argparse.Namespace) -> int:
    cmd = getattr(args, "workflow_command", None)
    if cmd is None:
        print("Usage: noui workflow {record,list,export-mcp}")
        return 1
    dispatch = {
        "record": cmd_workflow_record,
        "list": cmd_workflow_list,
        "captures": cmd_workflow_captures,
        "export-mcp": cmd_workflow_export_mcp,
    }
    fn = dispatch.get(cmd)
    if fn is None:
        print(_red(f"Unknown workflow subcommand: {cmd}"))
        return 1
    return fn(args)


def _dispatch_mcp(args: argparse.Namespace) -> int:
    cmd = getattr(args, "mcp_command", None)
    if cmd is None:
        print("Usage: noui mcp {list,status,start,stop}")
        return 1
    dispatch = {
        "list": cmd_mcp_list,
        "status": cmd_mcp_status,
        "start": cmd_mcp_start,
        "stop": cmd_mcp_stop,
    }
    fn = dispatch.get(cmd)
    if fn is None:
        print(_red(f"Unknown mcp subcommand: {cmd}"))
        return 1
    return fn(args)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Ensure defaults for optional flags used in dispatch
    for attr, default in [("validate", False), ("profile", "")]:
        if not hasattr(args, attr):
            setattr(args, attr, default)

    dispatch = {
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "login": _dispatch_login,
        "workflow": _dispatch_workflow,
        "mcp": _dispatch_mcp,
    }

    fn = dispatch.get(args.command)
    if fn is None:
        print(_red(f"Unknown command: {args.command}"))
        parser.print_help()
        sys.exit(1)

    rc = fn(args)
    sys.exit(rc if isinstance(rc, int) else 0)


if __name__ == "__main__":
    main()
