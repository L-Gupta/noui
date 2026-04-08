#!/usr/bin/env python3
"""
NoUI developer CLI — backend lifecycle, login recording, workflow recording, MCP servers, and Tabby.

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
    mcp install <server_id> <agent>    - Install MCP server into agent config (claude-desktop, claude-code, codex, opencode)

    tabby status                       - Check Docker Compose services and Tabby API liveness
    tabby start                        - Start Docker Compose infra and Tabby API
    tabby stop [--infra]               - Stop the Tabby API process (and optionally Docker Compose)
    tabby setup [--profiles] [--force] - Full provisioning: agent client + ServiceProfiles + .env
    tabby session status [--profile]   - Show browser session state for configured profiles
    tabby session ensure [--profile]   - Ensure a HEALTHY browser session exists
    tabby session stop [--profile]     - Stop the locally-running worker process
"""

from __future__ import annotations

import argparse
import base64
import getpass
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
from urllib.parse import urlparse

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
ENV_EXAMPLE = TABBY_DIR / ".env.example"

TABBY_PID_FILE = TABBY_DIR / ".tabby-api.pid"
TABBY_LOG_FILE = TABBY_DIR / ".tabby-api.log"
TABBY_WORKER_PID_FILE = TABBY_DIR / ".tabby-worker.pid"
TABBY_WORKER_LOG_FILE = TABBY_DIR / ".tabby-worker.log"
TABBY_CREDS_CACHE = TABBY_DIR / ".tabby-noui-client.json"
TABBY_AGENT_CLIENT_NAME = "noui"

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
    profile_id: str = getattr(args, "profile", "")
    profile_slug: str = getattr(args, "profile_slug", "")
    profile_db_id: str = getattr(args, "profile_db_id", "")
    capture_session_id: str = getattr(args, "capture_session", "")
    do_verify: bool = getattr(args, "verify", False)

    params = []
    if profile_id:
        params.append(f"tabby_profile_id={profile_id}")
    if profile_slug:
        params.append(f"profile_slug={profile_slug}")
    if profile_db_id:
        params.append(f"profile_db_id={profile_db_id}")
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

    print()
    print(_bold("MCP server generated:"))
    print(f"  Server ID  : {_cyan(server_id)}")
    print(f"  Tools      : {tool_count}")
    auth_info = result.get("auth", {})
    if auth_info.get("requires_auth"):
        strategy = auth_info.get("strategy") or "tabby_credentials"
        slug = auth_info.get("profile_slug") or auth_info.get("tabby_profile_id") or "?"
        print(f"  Auth       : {strategy} (profile: {slug})")
    else:
        print(f"  Auth       : none (public API)")
    print()

    if do_verify and server_id != "?":
        print(f"Running auth verification for {_cyan(server_id)} …")
        rc = _run_mcp_verify(server_id)
        if rc != 0:
            return rc

    print(f"  Install: {_bold(f'noui mcp install {server_id} claude-code')}")
    return 0


def _run_mcp_verify(server_id: str) -> int:
    """Run auth verification for a compiled MCP server."""
    import asyncio

    manifest_path = _find_mcp_manifest(server_id)
    if not manifest_path:
        print(_red(f"  Server {server_id!r} not found in mcp_servers/"))
        return 1

    server_dir = manifest_path.parent
    auth_plan_path = server_dir / "auth_plan.json"
    if not auth_plan_path.exists():
        print(_green("  No auth_plan.json — server is public, no verification needed."))
        return 0

    try:
        from compiler.mcp.auth_verifier import verify_before_install
    except ImportError as exc:
        print(_red(f"  Cannot import auth_verifier: {exc}"))
        return 1

    try:
        result = asyncio.run(verify_before_install(server_dir))
    except Exception as exc:
        print(_red(f"  Verification error: {exc}"))
        return 1

    status = result.status
    if status == "PASS":
        print(_green(f"  Auth verification PASSED: {result.message}"))
        return 0
    elif status == "REPAIR_APPLIED":
        print(_yellow(f"  Auth repair applied: {result.message}"))
        print(_yellow("  Re-run `noui mcp verify` after completing the suggested repairs."))
        for repair in result.suggested_repairs:
            cmd = repair.get("command") or repair.get("action", "")
            if cmd:
                print(f"    → {cmd}")
        return 0
    elif status == "NEEDS_SECRET":
        print(_red(f"  Auth verification FAILED — missing secrets:"))
        print(f"  {result.message}")
        for repair in result.suggested_repairs:
            cmd = repair.get("command") or ""
            var = repair.get("env_var") or ""
            if cmd:
                print(f"    → Run: {cmd}")
            elif var:
                print(f"    → Set: {var}=<value> in noui/.env")
        return 1
    else:
        print(_red(f"  Auth verification UNSUPPORTED: {result.message}"))
        return 1


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
# MCP install helpers
# ---------------------------------------------------------------------------


def _install_claude_desktop(
    server_id: str,
    server_dir: Path,
    python_cmd: str,
    server_script: Path,
    force: bool,
) -> int:
    config_path = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
    config: dict = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except Exception as exc:
            print(_red(f"Failed to parse {config_path}: {exc}"))
            return 1

    mcp_servers = config.setdefault("mcpServers", {})

    if server_id in mcp_servers and not force:
        print(_yellow(f"'{server_id}' is already configured in Claude Desktop. Use --force to overwrite."))
        return 0

    mcp_servers[server_id] = {
        "command": python_cmd,
        "args": [str(server_script)],
        "cwd": str(server_dir),
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    print(_green(f"Installed '{server_id}' into Claude Desktop ({config_path})"))
    print("  Restart Claude Desktop for changes to take effect.")
    return 0


def _install_claude_code(
    server_id: str,
    server_dir: Path,
    python_cmd: str,
    server_script: Path,
    force: bool,
) -> int:
    config_path = Path.home() / ".claude.json"
    config: dict = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except Exception as exc:
            print(_red(f"Failed to parse {config_path}: {exc}"))
            return 1

    mcp_servers = config.setdefault("mcpServers", {})

    if server_id in mcp_servers and not force:
        print(_yellow(f"'{server_id}' is already configured in Claude Code. Use --force to overwrite."))
        return 0

    mcp_servers[server_id] = {
        "type": "stdio",
        "command": python_cmd,
        "args": [str(server_script)],
        "cwd": str(server_dir),
    }

    config_path.write_text(json.dumps(config, indent=2) + "\n")
    print(_green(f"Installed '{server_id}' into Claude Code ({config_path})"))
    print("  Restart Claude Code (or run /mcp) for changes to take effect.")
    return 0


def _install_codex(
    server_id: str,
    server_dir: Path,  # noqa: ARG001
    python_cmd: str,
    server_script: Path,
    force: bool,
) -> int:
    import re

    config_path = Path.home() / ".codex" / "config.toml"
    text = config_path.read_text() if config_path.exists() else ""

    section_header = f"[mcp_servers.{server_id}]"
    already_exists = section_header in text

    if already_exists and not force:
        print(_yellow(f"'{server_id}' is already configured in Codex CLI. Use --force to overwrite."))
        return 0

    new_block = (
        f"\n[mcp_servers.{server_id}]\n"
        f'command = "{python_cmd}"\n'
        f'args = ["{server_script}"]\n'
    )

    if already_exists and force:
        # Remove the existing section (from header to the next section or EOF)
        pattern = re.compile(
            r"\n\[mcp_servers\." + re.escape(server_id) + r"\][^\[]*",
            re.DOTALL,
        )
        text = pattern.sub("", text)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text.rstrip("\n") + new_block)
    print(_green(f"Installed '{server_id}' into Codex CLI ({config_path})"))
    print("  Restart Codex for changes to take effect.")
    return 0


def _install_opencode(
    server_id: str,
    server_dir: Path,  # noqa: ARG001
    python_cmd: str,
    server_script: Path,
    force: bool,
) -> int:
    config_path = Path.home() / ".config" / "opencode" / "opencode.json"
    config: dict = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except Exception as exc:
            print(_red(f"Failed to parse {config_path}: {exc}"))
            return 1

    mcp_servers = config.setdefault("mcp", {})

    if server_id in mcp_servers and not force:
        print(_yellow(f"'{server_id}' is already configured in OpenCode. Use --force to overwrite."))
        return 0

    mcp_servers[server_id] = {
        "type": "local",
        "command": [python_cmd, str(server_script)],
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    print(_green(f"Installed '{server_id}' into OpenCode ({config_path})"))
    print("  Restart OpenCode for changes to take effect.")
    return 0


def cmd_mcp_install(args: argparse.Namespace) -> int:
    """Install a generated MCP server into an agent's config."""
    server_id: str = args.server_id
    agent: str = args.agent
    force: bool = getattr(args, "force", False)

    manifest_path = _find_mcp_manifest(server_id)
    if manifest_path is None:
        print(_red(f"MCP server '{server_id}' not found in {MCP_SERVERS_DIR}"))
        return 1

    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as exc:
        print(_red(f"Failed to parse manifest: {exc}"))
        return 1

    server_dir = manifest_path.parent
    runtime = manifest.get("runtime", {})
    entrypoint = (runtime.get("entrypoint") if isinstance(runtime, dict) else None) or "server.py"
    server_script = server_dir / entrypoint

    if not server_script.exists():
        print(_red(f"Server entrypoint not found: {server_script}"))
        return 1

    venv_python = NOUI_DIR / ".venv" / "bin" / "python"
    python_cmd = str(venv_python) if venv_python.exists() else sys.executable

    installers = {
        "claude-desktop": _install_claude_desktop,
        "claude-code": _install_claude_code,
        "codex": _install_codex,
        "opencode": _install_opencode,
    }
    return installers[agent](server_id, server_dir, python_cmd, server_script, force)


# ---------------------------------------------------------------------------
# mcp verify / diagnose-auth
# ---------------------------------------------------------------------------


def cmd_mcp_verify(args: argparse.Namespace) -> int:
    """Verify auth for a compiled MCP server before installation."""
    server_id: str = args.server_id
    return _run_mcp_verify(server_id)


def cmd_mcp_diagnose_auth(args: argparse.Namespace) -> int:
    """Diagnose auth issues for a compiled MCP server and suggest repairs."""
    import asyncio
    import json as _json

    server_id: str = args.server_id
    manifest_path = _find_mcp_manifest(server_id)
    if not manifest_path:
        print(_red(f"Server {server_id!r} not found in mcp_servers/"))
        return 1

    server_dir = manifest_path.parent
    auth_plan_path = server_dir / "auth_plan.json"

    # Show manifest auth section
    try:
        manifest = _json.loads(manifest_path.read_text())
    except Exception as exc:
        print(_red(f"Failed to read manifest: {exc}"))
        return 1

    print(_bold(f"Auth diagnosis for {_cyan(server_id)}"))
    print()
    auth_meta = manifest.get("auth", {})
    print(_bold("Manifest auth:"))
    for k, v in auth_meta.items():
        print(f"  {k:<20} {v}")
    print()

    if not auth_plan_path.exists():
        print(_yellow("No auth_plan.json found — server has no auth requirements."))
        return 0

    try:
        auth_plan = _json.loads(auth_plan_path.read_text())
    except Exception as exc:
        print(_red(f"Failed to read auth_plan.json: {exc}"))
        return 1

    print(_bold("Auth plan:"))
    strategy = auth_plan.get("strategy", "none")
    profile_slug = auth_plan.get("profile_slug", "")
    required = auth_plan.get("required_auth", {})
    fallbacks = auth_plan.get("fallbacks", [])

    print(f"  strategy       : {strategy}")
    print(f"  profile_slug   : {profile_slug or '(none)'}")
    print(f"  required headers: {required.get('headers', [])}")
    print(f"  required cookies: {required.get('cookies', [])}")
    if fallbacks:
        print(f"  fallbacks      :")
        for fb in fallbacks:
            if fb.get("type") == "static_secret_header":
                env_var = fb.get("secret_env_var", "")
                val = fb.get("value_template", "")
                present = "✓" if __import__("os").environ.get(env_var) else "✗ MISSING"
                print(f"    {fb['header']}: {val} [{env_var}={present}]")
    print()

    # Run verification
    print(_bold("Running verification …"))
    try:
        from compiler.mcp.auth_verifier import verify_before_install
        result = asyncio.run(verify_before_install(server_dir))
    except Exception as exc:
        print(_red(f"Verification error: {exc}"))
        return 1

    status_color = _green if result.status == "PASS" else (_yellow if result.status == "REPAIR_APPLIED" else _red)
    print(f"  Status: {status_color(result.status)}")
    print(f"  {result.message}")
    if result.missing_artifacts:
        print(f"  Missing: {', '.join(result.missing_artifacts)}")
    if result.suggested_repairs:
        print()
        print(_bold("  Suggested repairs:"))
        for repair in result.suggested_repairs:
            cmd = repair.get("command") or ""
            var = repair.get("env_var") or ""
            instructions = repair.get("instructions") or ""
            if cmd:
                print(f"    → {cmd}")
            elif instructions:
                print(f"    → {instructions}")
            elif var:
                print(f"    → Set {var}=<value> in noui/.env")
    return 0 if result.status in ("PASS", "REPAIR_APPLIED") else 1


# ---------------------------------------------------------------------------
# Tabby cache helpers
# ---------------------------------------------------------------------------


def _load_cache() -> dict[str, Any]:
    if not TABBY_CREDS_CACHE.exists():
        return {}
    try:
        return json.loads(TABBY_CREDS_CACHE.read_text())
    except Exception:
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    TABBY_CREDS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TABBY_CREDS_CACHE.write_text(json.dumps(cache, indent=2) + "\n")


def _write_env_vars(env_file: Path, updates: dict[str, str]) -> None:
    """Upsert key=value lines in env_file, creating it if needed."""
    existing_lines: list[str] = []
    if env_file.exists():
        existing_lines = env_file.read_text().splitlines()
    replaced: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                replaced.add(key)
                continue
        new_lines.append(line)
    for key, val in updates.items():
        if key not in replaced:
            new_lines.append(f"{key}={val}")
    env_file.write_text("\n".join(new_lines) + "\n")


# ---------------------------------------------------------------------------
# Tabby Docker Compose helpers
# ---------------------------------------------------------------------------


def _docker_compose_services() -> dict[str, str]:
    try:
        out = subprocess.check_output(
            ["docker", "compose", "ps", "--format", "json"],
            cwd=str(TABBY_DIR),
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        services: dict[str, str] = {}
        for line in out.decode().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                name = entry.get("Service") or entry.get("Name", "?")
                state = entry.get("State") or entry.get("Status", "?")
                services[name] = state
            except json.JSONDecodeError:
                continue
        return services
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Tabby provisioning helpers
# ---------------------------------------------------------------------------


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        segment = token.split(".")[1]
        segment += "=" * (4 - len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(segment))
    except Exception as exc:
        raise RuntimeError(f"Could not decode JWT payload: {exc}") from exc


def _secret_name(profile_id: str) -> str:
    return f"tabby-noui-{profile_id.lower().replace('_', '-')}"


def _env_prefix(secret_name: str) -> str:
    return secret_name.upper().replace("-", "_")


def _find_active_profile(profile_id: str, admin_token: str) -> dict[str, Any] | None:
    try:
        resp = _tabby_http("GET", "/admin/profiles?limit=200", token=admin_token)
        profiles: list[dict[str, Any]] = (
            resp.get("data", []) if isinstance(resp, dict) else list(resp)  # type: ignore[union-attr]
        )
        return next(
            (p for p in profiles if p.get("profile_id") == profile_id and p.get("version_state") == "ACTIVE"),
            None,
        )
    except RuntimeError:
        return None


def _prompt_profiles() -> list[str]:
    print()
    print(_bold("  First-time setup — enter your Tabby profile ID(s)"))
    print()
    print("  A profile ID is the identifier of a target-app credential set in Tabby.")
    print("  Examples: salesforce-standard, google-workspace, servicenow-itsm")
    print()
    while True:
        try:
            raw = input("  Profile ID(s) (space-separated): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return []
        profiles = [p.strip() for p in raw.split() if p.strip()]
        if profiles:
            return profiles
        print(_yellow("  At least one profile ID is required. Try again."))


def _load_cached_default_profiles() -> list[str]:
    cached = _load_cache()
    profiles = cached.get("default_profiles", [])
    return profiles if isinstance(profiles, list) else []


def _bypass_canary_gate(profile_db_id: str) -> bool:
    sql = (
        f"UPDATE service_profiles "
        f"SET canary_request_count=5, canary_error_count=0 "
        f"WHERE id='{profile_db_id}'"
    )
    try:
        subprocess.run(
            ["docker", "compose", "exec", "-T", "postgres", "psql",
             "-U", "browser_hitl", "-d", "browser_hitl", "-c", sql],
            cwd=str(TABBY_DIR),
            check=True,
            capture_output=True,
        )
        return True
    except Exception as exc:
        print(_red(f"Canary bypass failed: {exc}"))
        return False


def _prompt_app_config(profile_id: str) -> dict[str, Any] | None:
    print()
    print(_bold(f"  Configure login for profile '{profile_id}'"))
    print()
    print("  Tabby needs to know how to log into your target app so it can")
    print("  maintain a live browser session and serve fresh credentials.")
    print()
    try:
        login_url = input("  Login page URL (e.g. https://app.example.com/login): ").strip()
        if not login_url:
            return None

        print()
        print("  Leave email and password blank if the app requires no login.")
        username = input("  Test account email / username (optional): ").strip()
        password = ""
        if username:
            password = getpass.getpass("  Test account password: ")

        print()
        print("  CSS selectors for the login form (Enter = use default):")
        email_sel = (
            input("  Email/username field  [input[name='email'], input[type='email']]: ").strip()
            or "input[name='email'], input[type='email']"
        )
        pass_sel = (
            input("  Password field        [input[type='password']]: ").strip()
            or "input[type='password']"
        )
        submit_sel = (
            input("  Submit button         [button[type='submit']]: ").strip()
            or "button[type='submit']"
        )
        success_sel = input(
            "  Post-login element    (e.g. #dashboard, .user-menu — optional): "
        ).strip()

        otp = input("\n  Does login require OTP / MFA? [y/N]: ").strip().lower() == "y"
        otp_sel = ""
        if otp:
            otp_sel = (
                input("  OTP input selector    [input[name='otp']]: ").strip()
                or "input[name='otp']"
            )
    except (EOFError, KeyboardInterrupt):
        print()
        return None

    return {
        "login_url": login_url,
        "username": username,
        "password": password,
        "email_sel": email_sel,
        "pass_sel": pass_sel,
        "submit_sel": submit_sel,
        "success_sel": success_sel,
        "otp_required": otp,
        "otp_sel": otp_sel,
    }


def _build_app_payload(profile_id: str, cfg: dict[str, Any]) -> dict[str, Any]:
    parsed = urlparse(cfg["login_url"])
    origin = f"{parsed.scheme}://{parsed.netloc}"
    secret = _secret_name(profile_id)
    requires_login = bool(cfg.get("username"))

    steps: list[dict[str, Any]] = [{"action": "goto", "url": cfg["login_url"]}]
    if requires_login:
        steps += [
            {"action": "fill", "selector": cfg["email_sel"], "value": "${USERNAME}"},
            {"action": "fill", "selector": cfg["pass_sel"], "value": "${PASSWORD}", "sensitive": True},
            {"action": "click", "selector": cfg["submit_sel"]},
        ]
        if cfg.get("otp_required") and cfg.get("otp_sel"):
            steps += [
                {"action": "wait_for", "selector": cfg["otp_sel"], "timeout_ms": 120000, "sensitive": True},
                {"action": "click", "selector": "[type='submit']"},
            ]
    if cfg.get("success_sel"):
        steps.append({"action": "wait_for", "selector": cfg["success_sel"], "timeout_ms": 30000})

    credential_ref = f"k8s:secret/{secret}" if requires_login else "k8s:secret/no-auth"
    login_config: dict[str, Any] = {
        "login_url": cfg["login_url"],
        "credential_ref": credential_ref,
        "steps": steps,
    }
    if requires_login and cfg.get("otp_required") and cfg.get("otp_sel"):
        login_config["otp_prompt"] = {"method": "chat", "field_selector": cfg["otp_sel"]}

    return {
        "name": profile_id,
        "target_urls": [origin],
        "login_config": login_config,
        "keepalive_config": {
            "interval_seconds": 300,
            "actions": [],
            "health_checks": [{"type": "url_check", "url": origin, "expect_status": 200}],
            "policy": "all",
        },
        "export_policy": {
            "artifact_types": ["cookies", "headers", "csrf_token"],
            "encryption": {"algo": "AES-256-GCM", "key_version": "v1"},
            "ttl_seconds": 3600,
        },
        "notification_config": {"channels": ["slack:#local-dev"]},
        "desired_session_count": 0,
        "browser_policy": {"streaming_mode": "cdp"},
    }


def _ensure_service_profile(
    profile_id: str,
    tenant_id: str,
    admin_token: str,
    cache: dict[str, Any],
) -> bool:
    existing = _find_active_profile(profile_id, admin_token)
    if existing:
        print(_green(f"  ✓ '{profile_id}' is already ACTIVE in Tabby"))
        apps = cache.setdefault("apps", {})
        entry = apps.setdefault(profile_id, {})
        entry["app_id"] = existing.get("app_id", entry.get("app_id", ""))
        entry["profile_db_id"] = existing.get("id", entry.get("profile_db_id", ""))
        return True

    print(_yellow(f"  '{profile_id}' is not ACTIVE — configuring now…"))
    apps = cache.setdefault("apps", {})
    entry = apps.setdefault(profile_id, {})
    app_id: str = entry.get("app_id", "")

    if not app_id:
        cfg = _prompt_app_config(profile_id)
        if not cfg:
            print(_yellow(f"  Skipped '{profile_id}' — no config entered."))
            return False

        app_payload = _build_app_payload(profile_id, cfg)
        print(f"  Creating Application '{profile_id}' …", end=" ", flush=True)
        try:
            app_resp = _tabby_http("POST", "/apps", app_payload, token=admin_token)
            assert isinstance(app_resp, dict)
            app_id = app_resp["app_id"]
            print(_green("✓"))
        except (RuntimeError, KeyError, AssertionError) as exc:
            print()
            print(_red(f"  App creation failed: {exc}"))
            return False

        entry.update({
            "app_id": app_id,
            "login_url": cfg["login_url"],
            "username": cfg.get("username", ""),
            "credential_ref": app_payload["login_config"]["credential_ref"],
            "login_config": app_payload["login_config"],
        })
        if cfg.get("username") and cfg.get("password"):
            secret = _secret_name(profile_id)
            prefix = _env_prefix(secret)
            _write_env_vars(ENV_LOCAL, {
                f"{prefix}_USERNAME": cfg["username"],
                f"{prefix}_PASSWORD": cfg["password"],
            })

    login_config = entry.get("login_config", {})

    t = time.localtime()
    version = f"{t.tm_year % 100}.{t.tm_mon}.{t.tm_mday}"
    profile_payload: dict[str, Any] = {
        "profile_id": profile_id,
        "app_id": app_id,
        "version": version,
        "login_config": login_config,
        "credential_types": {"cookies": [], "headers": []},
        "target_domains": [urlparse(entry.get("login_url", "")).netloc or profile_id],
    }
    print(f"  Creating ServiceProfile '{profile_id}' …", end=" ", flush=True)
    try:
        prof_resp = _tabby_http("POST", "/admin/profiles", profile_payload, token=admin_token)
        assert isinstance(prof_resp, dict)
        profile_db_id: str = prof_resp["id"]
        print(_green("✓"))
    except (RuntimeError, KeyError, AssertionError) as exc:
        print()
        print(_red(f"  ServiceProfile creation failed: {exc}"))
        return False

    entry["profile_db_id"] = profile_db_id

    print("  Promoting STAGING → CANARY …", end=" ", flush=True)
    try:
        _tabby_http("POST", f"/admin/profiles/{profile_db_id}/promote", token=admin_token)
        print(_green("✓"))
    except RuntimeError as exc:
        print()
        print(_red(f"  Promotion failed: {exc}"))
        return False

    print("  Bypassing canary gate …", end=" ", flush=True)
    if not _bypass_canary_gate(profile_db_id):
        return False
    print(_green("✓"))

    print("  Promoting CANARY → ACTIVE …", end=" ", flush=True)
    try:
        _tabby_http("POST", f"/admin/profiles/{profile_db_id}/promote", token=admin_token)
        print(_green("✓"))
    except RuntimeError as exc:
        print()
        print(_red(f"  Promotion to ACTIVE failed: {exc}"))
        return False

    return True


# ---------------------------------------------------------------------------
# Tabby session helpers
# ---------------------------------------------------------------------------


def _get_sessions(admin_token: str) -> list[dict[str, Any]]:
    try:
        resp = _tabby_http("GET", "/sessions?limit=200", token=admin_token)
        if isinstance(resp, dict):
            return resp.get("data", [])
        return list(resp)  # type: ignore[arg-type]
    except RuntimeError:
        return []


def _seed_session(app_id: str, tenant_id: str) -> str | None:
    seed_script = TABBY_DIR / "scripts" / "batch-a-seed-session.js"
    if not seed_script.exists():
        print(_red(f"Seed script not found: {seed_script}"))
        return None

    env = {**os.environ}
    env.update(_load_env_local())

    print("  Seeding session record …", end=" ", flush=True)
    try:
        result = subprocess.run(
            ["node", str(seed_script), app_id, tenant_id],
            cwd=str(TABBY_DIR),
            env=env,
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            print()
            print(_red(f"Seed script failed: {result.stderr.decode(errors='replace')}"))
            return None
        data = json.loads(result.stdout.decode())
        session_id: str = data["session"]["id"]
        print(_green("✓"))
        return session_id
    except Exception as exc:
        print()
        print(_red(f"Seed script error: {exc}"))
        return None


# ---------------------------------------------------------------------------
# tabby subcommands
# ---------------------------------------------------------------------------


def cmd_tabby_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    print(_bold("Tabby infrastructure:"))
    services = _docker_compose_services()
    if services:
        for name, state in services.items():
            ok = "running" in state.lower()
            icon = _green("✓") if ok else _red("✗")
            print(f"  {icon}  {name}: {state}")
    else:
        print(_yellow("  (could not reach Docker Compose — is Docker running?)"))
    print()
    print(_bold("Tabby API:"))
    if _tabby_alive():
        pid = _read_pid(TABBY_PID_FILE)
        pid_label = f" (PID {pid})" if pid else ""
        print(_green(f"  ✓  API ready at {TABBY_API_HOST}{pid_label}"))
        return 0
    else:
        print(_red(f"  ✗  API not reachable at {TABBY_API_HOST}"))
        print(f"     Run: {_bold('noui tabby start')}")
        return 1


def cmd_tabby_start(args: argparse.Namespace) -> int:  # noqa: ARG001
    if _tabby_alive():
        pid = _read_pid(TABBY_PID_FILE)
        pid_label = f" (PID {pid})" if pid else ""
        print(_yellow(f"Tabby API is already running{pid_label} at {TABBY_API_HOST}"))
        return 0

    if not TABBY_DIR.exists():
        print(_red(f"Tabby directory not found: {TABBY_DIR}"))
        return 1

    if not ENV_LOCAL.exists():
        if ENV_EXAMPLE.exists():
            import shutil
            shutil.copy(ENV_EXAMPLE, ENV_LOCAL)
            print(_yellow(f"Created {ENV_LOCAL} from template."))
            print(_yellow("Edit it and set JWT_SIGNING_KEY, TENANT_ENCRYPTION_KEY,"))
            print(_yellow("AGENT_SECRET_HMAC_KEY, ADMIN_BOOTSTRAP_EMAIL, ADMIN_BOOTSTRAP_PASSWORD."))
            print()
        else:
            print(_red(f"No .env.local found at {ENV_LOCAL}"))
            return 1

    print("Starting Docker Compose infrastructure …", end="", flush=True)
    try:
        subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=str(TABBY_DIR),
            check=True,
            capture_output=True,
        )
        print(_green(" ✓"))
    except subprocess.CalledProcessError as exc:
        print()
        print(_red(f"docker compose up failed: {exc.stderr.decode(errors='replace')}"))
        return 1
    except FileNotFoundError:
        print()
        print(_red("'docker' command not found. Is Docker installed and in PATH?"))
        return 1

    env = {**os.environ}
    env.update(_load_env_local())

    TABBY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(TABBY_LOG_FILE, "a")  # noqa: SIM115
    proc = subprocess.Popen(
        ["pnpm", "--filter", "@browser-hitl/api", "start:dev"],
        cwd=str(TABBY_DIR),
        env=env,
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
    )
    TABBY_PID_FILE.write_text(str(proc.pid))
    print(f"Starting Tabby API (PID {proc.pid}) … logs → {_cyan(str(TABBY_LOG_FILE))}", end="", flush=True)

    for _ in range(60):
        time.sleep(1)
        print(".", end="", flush=True)
        if _tabby_alive():
            break
    else:
        print()
        print(_red(f"API did not become ready within 60s. Check logs: {TABBY_LOG_FILE}"))
        _clear_pid(TABBY_PID_FILE)
        return 1

    print()
    print(_green(f"✓ Tabby API ready at {TABBY_API_HOST}"))
    print()
    print(f"  Next: {_bold('noui tabby setup')}")
    return 0


def cmd_tabby_stop(args: argparse.Namespace) -> int:
    pid = _read_pid(TABBY_PID_FILE)
    if pid is None:
        if _tabby_alive():
            print(_yellow("API is running but PID file not found — stop it manually."))
            return 1
        print(_yellow("Tabby API is not running."))
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(15):
                time.sleep(0.5)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            _clear_pid(TABBY_PID_FILE)
            print(_green(f"✓ API process (PID {pid}) stopped."))
        except ProcessLookupError:
            _clear_pid(TABBY_PID_FILE)
            print(_yellow(f"Process {pid} was not running — cleared stale PID file."))
        except Exception as exc:
            print(_red(f"Failed to stop API: {exc}"))
            return 1

    if args.infra:
        print("Stopping Docker Compose infrastructure …", end="", flush=True)
        try:
            subprocess.run(
                ["docker", "compose", "stop"],
                cwd=str(TABBY_DIR),
                check=True,
                capture_output=True,
            )
            print(_green(" ✓"))
        except Exception as exc:
            print()
            print(_red(f"docker compose stop failed: {exc}"))
            return 1
    return 0


def cmd_tabby_setup(args: argparse.Namespace) -> int:
    """Full end-to-end Tabby provisioning for NoUI."""
    if not _tabby_alive():
        print("Tabby API is not running — starting it first …")
        print()
        rc = cmd_tabby_start(args)
        if rc != 0:
            return rc
        print()

    env_local = _load_env_local()
    admin_email = env_local.get("ADMIN_BOOTSTRAP_EMAIL", "")
    admin_password = env_local.get("ADMIN_BOOTSTRAP_PASSWORD", "")
    if not admin_email or not admin_password:
        print(_red(f"ADMIN_BOOTSTRAP_EMAIL and ADMIN_BOOTSTRAP_PASSWORD must be set in {ENV_LOCAL}"))
        return 1

    print(f"Logging in as {_cyan(admin_email)} …", end=" ", flush=True)
    try:
        login_resp = _tabby_http("POST", "/login", {"email": admin_email, "password": admin_password})
        assert isinstance(login_resp, dict)
        admin_token = login_resp.get("token") or login_resp.get("access_token", "")
    except (RuntimeError, AssertionError) as exc:
        print()
        print(_red(f"Login failed: {exc}"))
        return 1
    if not admin_token:
        print(_red(f"\nNo token in login response: {login_resp}"))
        return 1
    print(_green("✓"))

    payload = _decode_jwt_payload(admin_token)
    tenant_id = payload.get("tenant_id") or payload.get("tenantId") or payload.get("sub", "")
    if not tenant_id:
        print(_red(f"Could not find tenant_id in JWT: {payload}"))
        return 1
    print(f"Tenant ID: {_cyan(tenant_id)}")

    cache = _load_cache()

    if args.profiles:
        allowed_profiles = list(args.profiles)
        print(f"Profiles (from --profiles): {_cyan(', '.join(allowed_profiles))}")
    else:
        cached_defaults = _load_cached_default_profiles()
        if cached_defaults and not args.force:
            allowed_profiles = cached_defaults
            print(f"Profiles (saved default): {_cyan(', '.join(allowed_profiles))}")
        else:
            allowed_profiles = _prompt_profiles()
            if not allowed_profiles:
                print(_red("No profiles provided — setup cancelled."))
                return 1
            print(f"Profiles: {_cyan(', '.join(allowed_profiles))}")

    client_id: str = ""
    client_secret: str = ""

    if not args.force:
        client_id = cache.get("client_id", "")
        client_secret = cache.get("client_secret", "")
        if client_id and client_secret:
            print(f"Using cached agent client: {_cyan(client_id)}")
            if args.profiles and cache.get("default_profiles") != allowed_profiles:
                cache["default_profiles"] = allowed_profiles

    if not (client_id and client_secret):
        try:
            existing_clients = _tabby_http("GET", f"/admin/agent-clients/{tenant_id}", token=admin_token)
            if not isinstance(existing_clients, list):
                existing_clients = []
        except RuntimeError:
            existing_clients = []

        match = next((c for c in existing_clients if c.get("name") == TABBY_AGENT_CLIENT_NAME), None)

        if match and not args.force:
            print(f"Agent client '{TABBY_AGENT_CLIENT_NAME}' already exists — rotating secret …", end=" ", flush=True)
            try:
                rotated = _tabby_http(
                    "POST",
                    f"/admin/agent-clients/{match['id']}/rotate-secret",
                    token=admin_token,
                )
                assert isinstance(rotated, dict)
                client_id = rotated.get("client_id", match["client_id"])
                client_secret = rotated.get("client_secret", "")
                print(_green("✓"))
            except (RuntimeError, AssertionError) as exc:
                print()
                print(_red(f"Secret rotation failed: {exc}"))
                return 1
        else:
            action = "Force-recreating" if (match and args.force) else "Registering"
            print(f"{action} agent client '{TABBY_AGENT_CLIENT_NAME}' …", end=" ", flush=True)
            if match and args.force:
                try:
                    _tabby_http("DELETE", f"/admin/agent-clients/{match['id']}", token=admin_token)
                except RuntimeError:
                    pass
            try:
                created = _tabby_http(
                    "POST",
                    "/admin/agent-clients",
                    {
                        "name": TABBY_AGENT_CLIENT_NAME,
                        "tenant_id": tenant_id,
                        "allowed_profiles": allowed_profiles,
                        "token_ttl_seconds": 3600,
                    },
                    token=admin_token,
                )
                assert isinstance(created, dict)
                client_id = created["client_id"]
                client_secret = created["client_secret"]
                print(_green("✓"))
            except (RuntimeError, KeyError, AssertionError) as exc:
                print()
                print(_red(f"Agent client creation failed: {exc}"))
                return 1

        if not client_secret:
            print(_red("No client_secret in response — cannot proceed."))
            return 1

    cache.update({
        "client_id": client_id,
        "client_secret": client_secret,
        "default_profiles": allowed_profiles,
    })
    _save_cache(cache)

    print()
    print(_bold("Provisioning ServiceProfiles:"))
    for profile_id in allowed_profiles:
        cache = _load_cache()
        ok = _ensure_service_profile(profile_id, tenant_id, admin_token, cache)
        _save_cache(cache)
        if not ok:
            print(_yellow(f"  Skipped '{profile_id}' — re-run setup to configure it."))

    env_file = Path(args.env_file) if args.env_file else NOUI_DIR / ".env"
    _write_env_vars(env_file, {
        "TABBY_API_URL": TABBY_API_HOST,
        "TABBY_CLIENT_ID": client_id,
        "TABBY_CLIENT_SECRET": client_secret,
    })

    print()
    print(_green("✓ Setup complete!"))
    print()
    print(f"  Credentials written to: {_cyan(str(env_file))}")
    print(f"  Agent client:           {_cyan(client_id)}")
    print()
    print("  Next: ensure a browser session is running:")
    print(f"    {_bold('noui tabby session ensure')}")
    return 0


# ---------------------------------------------------------------------------
# tabby session subcommands
# ---------------------------------------------------------------------------


def cmd_session_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    if not _tabby_alive():
        print(_red(f"Tabby API is not running. Run: {_bold('noui tabby start')}"))
        return 1

    admin_token = _get_admin_token()
    if not admin_token:
        return 1

    sessions = _get_sessions(admin_token)
    cache = _load_cache()
    apps = cache.get("apps", {})
    app_to_profile = {v.get("app_id"): k for k, v in apps.items()}

    if not sessions:
        print(_yellow("No sessions found."))
        print(f"  Start one with: {_bold('noui tabby session ensure')}")
        return 0

    profile_filter: str | None = getattr(args, "profile", None)

    print(_bold("Browser sessions:"))
    shown = 0
    for s in sessions:
        app_id = s.get("app_id", "")
        profile_name = app_to_profile.get(app_id, app_id[:8] + "…")
        if profile_filter and profile_name != profile_filter:
            continue
        state = s.get("state", "?")
        sid = s.get("id", "?")
        if state == "HEALTHY":
            icon = _green("●")
        elif state in ("FAILED", "TERMINATED"):
            icon = _red("●")
        else:
            icon = _yellow("●")
        print(f"  {icon}  {_bold(profile_name)}: {state}  (id: {sid[:8]}…)")
        shown += 1

    if shown == 0:
        print(_yellow(f"  No sessions for profile '{profile_filter}'."))
    return 0


def cmd_session_ensure(args: argparse.Namespace) -> int:
    if not _tabby_alive():
        print(_red(f"Tabby API is not running. Run: {_bold('noui tabby start')}"))
        return 1

    admin_token = _get_admin_token()
    if not admin_token:
        return 1

    cache = _load_cache()
    apps = cache.get("apps", {})

    profile_id: str | None = getattr(args, "profile", None)
    if not profile_id:
        if len(apps) == 1:
            profile_id = list(apps.keys())[0]
        elif len(apps) > 1:
            print(_red("Multiple profiles configured. Specify one with --profile:"))
            for p in apps:
                print(f"  {p}")
            return 1
        else:
            defaults = cache.get("default_profiles", [])
            profile_id = defaults[0] if len(defaults) == 1 else None
        if not profile_id:
            print(_red("Could not determine profile. Run 'noui tabby setup' first or pass --profile."))
            return 1

    entry = apps.get(profile_id)
    if not entry:
        print(_red(f"Profile '{profile_id}' not found in cache. Run: noui tabby setup"))
        return 1

    app_id: str = entry.get("app_id", "")
    if not app_id:
        print(_red(f"No app_id cached for '{profile_id}'. Re-run: noui tabby setup"))
        return 1

    try:
        jwt_payload = _decode_jwt_payload(admin_token)
        tenant_id = jwt_payload.get("tenant_id") or jwt_payload.get("tenantId", "")
    except RuntimeError:
        tenant_id = ""

    sessions = _get_sessions(admin_token)
    healthy = [s for s in sessions if s.get("app_id") == app_id and s.get("state") == "HEALTHY"]
    if healthy:
        print(_green(f"✓ Session for '{profile_id}' is already HEALTHY"))
        return 0

    print(f"No HEALTHY session for '{_cyan(profile_id)}' — starting one …")

    session_id = _seed_session(app_id, tenant_id)
    if not session_id:
        return 1
    print(f"  Session ID: {_cyan(session_id)}")

    env = {**os.environ}
    env.update(_load_env_local())
    env.update({
        "SESSION_ID": session_id,
        "APP_ID": app_id,
        "TENANT_ID": tenant_id,
        "STREAMING_MODE": "cdp",
    })

    creds_mount = Path("/tmp/tabby-local-secrets")
    secret_name = entry.get("credential_ref", "k8s:secret/no-auth").replace("k8s:secret/", "")
    secret_dir = creds_mount / secret_name
    secret_dir.mkdir(parents=True, exist_ok=True)
    if entry.get("username"):
        env_local_vars = _load_env_local()
        prefix = _env_prefix(_secret_name(profile_id))
        username = entry["username"]
        password = env_local_vars.get(f"{prefix}_PASSWORD", "")
        if not password:
            print(_red(f"Password for '{profile_id}' not found in {ENV_LOCAL}."))
            print("Re-run: noui tabby setup")
            return 1
        (secret_dir / "username").write_text(username)
        (secret_dir / "password").write_text(password)
    else:
        (secret_dir / "username").write_text("no-auth")
        (secret_dir / "password").write_text("no-auth")
    env["CREDENTIALS_MOUNT_PATH"] = str(creds_mount)

    old_pid = _read_pid(TABBY_WORKER_PID_FILE)
    if old_pid:
        try:
            os.kill(old_pid, signal.SIGTERM)
            for _ in range(10):
                time.sleep(0.3)
                try:
                    os.kill(old_pid, 0)
                except ProcessLookupError:
                    break
        except ProcessLookupError:
            pass
        _clear_pid(TABBY_WORKER_PID_FILE)
    try:
        subprocess.run(["fuser", "-k", "8091/tcp"], capture_output=True)
        time.sleep(0.5)
    except FileNotFoundError:
        pass

    TABBY_WORKER_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    worker_log_fh = open(TABBY_WORKER_LOG_FILE, "a")  # noqa: SIM115
    proc = subprocess.Popen(
        ["pnpm", "--filter", "@browser-hitl/worker", "start"],
        cwd=str(TABBY_DIR),
        env=env,
        stdout=worker_log_fh,
        stderr=worker_log_fh,
        start_new_session=True,
    )
    TABBY_WORKER_PID_FILE.write_text(str(proc.pid))
    print(f"  Worker started (PID {proc.pid}) — logs → {_cyan(str(TABBY_WORKER_LOG_FILE))}")
    print()
    print("  Waiting for health check to pass", end="", flush=True)

    final_state = ""
    final_health = ""
    for _ in range(60):
        time.sleep(5)
        print(".", end="", flush=True)
        try:
            resp = _tabby_http("GET", f"/sessions/{session_id}", token=admin_token)
            assert isinstance(resp, dict)
            final_state = resp.get("state", "")
            final_health = resp.get("health_result_type", "")
            if final_state == "HEALTHY" or final_health == "PASS":
                break
            if final_state in ("FAILED", "TERMINATED") or final_health == "AUTH_FAIL":
                print()
                print(_red(f"Session failed (state={final_state}, health={final_health})."))
                print(f"  Worker logs: {TABBY_WORKER_LOG_FILE}")
                return 1
        except (RuntimeError, AssertionError):
            pass
    else:
        print()
        print(_red("Session did not pass health check within 5 minutes."))
        print(f"  Worker logs: {TABBY_WORKER_LOG_FILE}")
        return 1

    print()

    if final_state != "HEALTHY":
        print("  Promoting session state to HEALTHY …", end=" ", flush=True)
        sql = f"UPDATE sessions SET state='HEALTHY' WHERE id='{session_id}'"
        try:
            subprocess.run(
                ["docker", "compose", "exec", "-T", "postgres", "psql",
                 "-U", "browser_hitl", "-d", "browser_hitl", "-c", sql],
                cwd=str(TABBY_DIR),
                check=True,
                capture_output=True,
            )
            print(_green("✓"))
        except Exception as exc:
            print()
            print(_red(f"Failed to promote session state: {exc}"))
            return 1

    print(_green(f"✓ Session for '{profile_id}' is HEALTHY"))
    print()
    print("  You can now record a workflow with Tabby auth:")
    print(f"    {_bold('noui workflow record \"My Workflow\" <url>')}")
    return 0


def cmd_session_stop(args: argparse.Namespace) -> int:  # noqa: ARG001
    pid = _read_pid(TABBY_WORKER_PID_FILE)
    if pid is None:
        print(_yellow("No worker PID file found — worker may not be running."))
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        _clear_pid(TABBY_WORKER_PID_FILE)
        print(_green(f"✓ Worker (PID {pid}) stopped."))
        return 0
    except ProcessLookupError:
        _clear_pid(TABBY_WORKER_PID_FILE)
        print(_yellow(f"Process {pid} was not running — cleared stale PID file."))
        return 0
    except Exception as exc:
        print(_red(f"Failed to stop worker: {exc}"))
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
        help="Legacy: Tabby profile ID (UUID or slug). Prefer --profile-slug.",
    )
    wf_export.add_argument(
        "--profile-slug",
        default="",
        metavar="SLUG",
        dest="profile_slug",
        help="Tabby profile slug for runtime credential requests (e.g. 'adopt-bank')",
    )
    wf_export.add_argument(
        "--profile-db-id",
        default="",
        metavar="UUID",
        dest="profile_db_id",
        help="Tabby profile DB UUID for admin operations only",
    )
    wf_export.add_argument(
        "--capture-session",
        default="",
        metavar="CAPTURE_SESSION_ID",
        help="Use HAR/clicks from an ABCD capture session instead of the workflow session",
    )
    wf_export.add_argument(
        "--verify",
        action="store_true",
        default=False,
        help="Run auth verification after export; report PASS/NEEDS_SECRET before install",
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

    mcp_install_p = mcp_sub.add_parser("install", help="Install MCP server into an agent config")
    mcp_install_p.add_argument("server_id", help="MCP server ID")
    mcp_install_p.add_argument(
        "agent",
        choices=["claude-desktop", "claude-code", "codex", "opencode"],
        help="Target agent",
    )
    mcp_install_p.add_argument("--force", action="store_true", help="Overwrite existing configuration")

    mcp_verify_p = mcp_sub.add_parser(
        "verify",
        help="Verify auth for a compiled MCP server (checks Tabby creds, env vars, dry-run)",
    )
    mcp_verify_p.add_argument("server_id", help="MCP server ID")

    mcp_diagnose_p = mcp_sub.add_parser(
        "diagnose-auth",
        help="Show auth diagnosis and repair guidance for a compiled MCP server",
    )
    mcp_diagnose_p.add_argument("server_id", help="MCP server ID")

    # --- tabby ---
    tabby_parser = sub.add_parser("tabby", help="Tabby credential service lifecycle commands")
    tabby_sub = tabby_parser.add_subparsers(dest="tabby_command")

    tabby_sub.add_parser("status", help="Check Docker Compose services and Tabby API liveness")
    tabby_sub.add_parser("start", help="Start Docker Compose infra and Tabby API in background")

    tabby_stop_p = tabby_sub.add_parser("stop", help="Stop the Tabby API process")
    tabby_stop_p.add_argument("--infra", action="store_true", help="Also stop Docker Compose services")

    tabby_setup_p = tabby_sub.add_parser(
        "setup",
        help="Full provisioning: agent client + ServiceProfiles + write .env",
    )
    tabby_setup_p.add_argument(
        "--profiles",
        nargs="+",
        metavar="PROFILE_ID",
        default=None,
        help="Tabby profile IDs to provision (prompted interactively if omitted)",
    )
    tabby_setup_p.add_argument(
        "--force",
        action="store_true",
        help="Revoke and recreate the agent client even if one exists",
    )
    tabby_setup_p.add_argument(
        "--env-file",
        metavar="PATH",
        default=None,
        help="Path to write TABBY_* vars into (default: noui/.env)",
    )

    tabby_session_p = tabby_sub.add_parser("session", help="Manage browser sessions")
    tabby_session_sub = tabby_session_p.add_subparsers(dest="session_action")

    tabby_session_sub.add_parser("status", help="Show session state for configured profiles")

    ensure_p = tabby_session_sub.add_parser(
        "ensure",
        help="Ensure a HEALTHY session exists, starting the worker if needed",
    )
    ensure_p.add_argument(
        "--profile",
        metavar="PROFILE_ID",
        default=None,
        help="Profile to ensure (default: the only configured profile)",
    )

    stop_sess_p = tabby_session_sub.add_parser("stop", help="Stop the locally-running worker")
    stop_sess_p.add_argument(
        "--profile",
        metavar="PROFILE_ID",
        default=None,
        help="Profile whose worker to stop",
    )

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
        print("Usage: noui mcp {list,status,start,stop,install,verify,diagnose-auth}")
        return 1
    dispatch = {
        "list": cmd_mcp_list,
        "status": cmd_mcp_status,
        "start": cmd_mcp_start,
        "stop": cmd_mcp_stop,
        "install": cmd_mcp_install,
        "verify": cmd_mcp_verify,
        "diagnose-auth": cmd_mcp_diagnose_auth,
    }
    fn = dispatch.get(cmd)
    if fn is None:
        print(_red(f"Unknown mcp subcommand: {cmd}"))
        return 1
    return fn(args)


def _dispatch_tabby_session(args: argparse.Namespace) -> int:
    action = getattr(args, "session_action", None)
    if action is None:
        print("Usage: noui tabby session {status,ensure,stop}")
        return 1
    dispatch = {
        "status": cmd_session_status,
        "ensure": cmd_session_ensure,
        "stop": cmd_session_stop,
    }
    fn = dispatch.get(action)
    if fn is None:
        print(_red(f"Unknown session subcommand: {action}"))
        return 1
    return fn(args)


def _dispatch_tabby(args: argparse.Namespace) -> int:
    cmd = getattr(args, "tabby_command", None)
    if cmd is None:
        print("Usage: noui tabby {status,start,stop,setup,session}")
        return 1
    dispatch = {
        "status": cmd_tabby_status,
        "start": cmd_tabby_start,
        "stop": cmd_tabby_stop,
        "setup": cmd_tabby_setup,
        "session": _dispatch_tabby_session,
    }
    fn = dispatch.get(cmd)
    if fn is None:
        print(_red(f"Unknown tabby subcommand: {cmd}"))
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
    for attr, default in [
        ("validate", False),
        ("profile", None),
        ("infra", False),
        ("force", False),
        ("profiles", None),
        ("env_file", None),
    ]:
        if not hasattr(args, attr):
            setattr(args, attr, default)

    dispatch = {
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "login": _dispatch_login,
        "workflow": _dispatch_workflow,
        "mcp": _dispatch_mcp,
        "tabby": _dispatch_tabby,
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
