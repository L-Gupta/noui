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
    login credentials <bundle_file>    - Set username/password for a registered profile
    login import <session_id>          - export + review + register [+ validate]

    workflow record <name> <url>       - Create workflow session + print extension instructions
    workflow list                      - List workflow sessions
    workflow captures                  - List capture sessions recorded via the extension
    workflow export <session_id>       - Compile workflow to MCP, Skill, or both (--as mcp|skill|both)

    autopilot start-capture <name> <url> - Create sessions and start HAR/click capture
    autopilot stop-capture <wf> <cs>   - Stop capture, complete workflow, wait for HAR
    autopilot export <wf> <cs>         - Validate HAR and export MCP server
    autopilot browser <cmd> [args]     - Execute a browser command via the extension
    autopilot list                     - List autopilot recording runs
    autopilot status <run_id>          - Show status of an autopilot run

    mcp list                           - List generated MCP servers
    mcp status <server_id>             - Show status of a generated MCP server
    mcp start <server_id>              - Start a generated MCP server
    mcp stop <server_id>               - Stop a generated MCP server
    mcp install <server_id> <agent>    - Install MCP server into agent config (claude-desktop, claude-code, codex, opencode)

    skill list                         - List generated skills
    skill show <skill_id>              - Print skill manifest + SKILL.md preview
    skill install <skill_id> <agent>   - Copy skill to an agent's skills dir (claude-code|codex|cline|opencode|agents); add --project for project-scoped path
    skill uninstall <skill_id> <agent> - Remove an installed skill (use --project for project-scoped)

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
import threading
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
WORKBENCH_DIR = NOUI_DIR / "workbench"
MCP_SERVERS_DIR = WORKBENCH_DIR / "mcp_servers"
LOGIN_RECORDINGS_DIR = WORKBENCH_DIR / "login_recordings"
SKILLS_DIR = WORKBENCH_DIR / "skills"
NOUI_PID_FILE = NOUI_DIR / ".noui-backend.pid"
NOUI_LOG_FILE = NOUI_DIR / ".noui-backend.log"

NOUI_PORT = int(os.environ.get("NOUI_PORT", "8002"))
BACKEND_URL = f"http://localhost:{NOUI_PORT}"

TABBY_DIR = (
    Path(os.environ["TABBY_DIR"]).expanduser()
    if os.environ.get("TABBY_DIR")
    else NOUI_DIR / "tabby"
)
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


def _cdp_is_reachable(host: str = "localhost", port: int = 9222, timeout: float = 2.0) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _tabby_worker_build_state() -> tuple[str, str]:
    """Report the freshness of the Tabby worker's compiled output.

    Returns a tuple ``(state, detail)`` where ``state`` is one of:
        - ``"ok"``   — `dist/main.js` exists and is newer than every .ts file under src/.
        - ``"missing"`` — the worker has never been built (no dist/main.js).
        - ``"stale"`` — a source file is newer than the compiled output.
        - ``"no-worker"`` — the worker directory itself doesn't exist (e.g. fresh
          checkout without submodule init); this is not the normal build-state
          concern and callers should surface it differently.

    ``detail`` is a short human-readable explanation (what file is missing,
    which .ts triggered the stale signal, etc.) suitable for printing next to
    the state label.

    Used by `noui status`, `noui tabby setup`, and `noui tabby session ensure`
    to fail fast instead of letting the worker crash-loop and reporting only
    "Session did not pass health check within 5 minutes" after waiting.
    """
    worker_dir = TABBY_DIR / "apps" / "worker"
    if not worker_dir.exists():
        return ("no-worker", f"worker directory not found: {worker_dir}")

    main_js = worker_dir / "dist" / "main.js"
    if not main_js.exists():
        return ("missing", f"{main_js} not found — worker has never been built")

    src_dir = worker_dir / "src"
    if not src_dir.exists():
        # Build output exists but source dir is missing; treat as ok (we can't prove staleness).
        return ("ok", f"{main_js} present (no src/ to compare)")

    main_js_mtime = main_js.stat().st_mtime
    newest_src = main_js_mtime
    newest_path = ""
    for ts_file in src_dir.rglob("*.ts"):
        try:
            mtime = ts_file.stat().st_mtime
        except OSError:
            continue
        if mtime > newest_src:
            newest_src = mtime
            newest_path = str(ts_file.relative_to(worker_dir))

    if newest_src > main_js_mtime:
        return ("stale", f"{newest_path} is newer than dist/main.js")
    return ("ok", f"{main_js.relative_to(TABBY_DIR)} up to date")


def _tabby_worker_build_hint() -> str:
    """Return the exact shell command to fix a missing/stale worker build."""
    return f"pnpm install && pnpm nx build worker  # run inside {TABBY_DIR}"


# Patterns that indicate a worker cannot possibly become HEALTHY — short-circuit
# the 5-minute health-check wait and surface the failure.
_WORKER_FATAL_PATTERNS = (
    "MODULE_NOT_FOUND",
    "Cannot find module",
    "ERR_REQUIRE_ESM",
    "SyntaxError:",
    "ReferenceError:",
    "TypeError:",
    "UnhandledPromiseRejection",
    "address already in use",
    "EADDRINUSE",
    "ECONNREFUSED",  # worker's own deps (redis/postgres) unreachable
    "Error: connect ECONNREFUSED",
)

# Patterns that indicate forward progress — surface verbatim to the user so they
# see the worker actually booting instead of watching progress dots.
_WORKER_PROGRESS_PATTERNS = (
    "listening on",
    "Nest application successfully started",
    "Worker ready",
    "Health check passed",
    "Starting browser",
    "CloakBrowser",
)


def _navigate_cdp_page(url: str, *, timeout: float = 15.0) -> tuple[bool, str]:
    """Navigate the Tabby-managed Chrome tab to ``url`` via CDP.

    Finds the first page target at localhost:9222/json, sends Page.navigate,
    and waits briefly for the URL to settle. Returns ``(ok, detail)``.

    Used by `tabby session ensure --open ...` and `--skill ...` so users
    don't have to hand-write a Page.navigate call for every anonymous-session
    site whose cookies are provisioned on page load.
    """
    import json as _json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen("http://localhost:9222/json", timeout=5) as resp:
            targets = _json.load(resp)
    except (OSError, urllib.error.URLError) as exc:
        return (False, f"CDP endpoint unreachable at localhost:9222 ({exc})")

    page_target = next((t for t in targets if t.get("type") == "page"), None)
    if not page_target:
        return (False, "no page target found at localhost:9222/json")
    target_id = page_target.get("id", "")
    if not target_id:
        return (False, "page target has no id")

    # Send Page.navigate over the HTTP-to-WS bridge. Using websockets keeps the
    # dependency surface the same as noui_runtime/cdp.py (already vendored).
    try:
        import websockets  # type: ignore  # noqa: F401
    except ImportError:
        return (False, "websockets package not available in the CLI venv")

    async def _navigate() -> tuple[bool, str]:
        import websockets as _ws  # type: ignore

        ws_url = page_target.get("webSocketDebuggerUrl") or ""
        if not ws_url:
            return (False, "no webSocketDebuggerUrl on page target")
        try:
            async with _ws.connect(ws_url, max_size=10_000_000) as ws:
                await ws.send(
                    _json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": url}})
                )
                await asyncio.wait_for(ws.recv(), timeout=5)
        except Exception as exc:  # noqa: BLE001
            return (False, f"CDP navigate failed: {exc}")
        return (True, f"navigated to {url}")

    import asyncio

    try:
        return asyncio.run(asyncio.wait_for(_navigate(), timeout=timeout))
    except TimeoutError:
        return (False, f"navigation timed out after {timeout}s")


def _skill_manifest_start_url(skill_id: str) -> tuple[str, str]:
    """Look up `workflow.start_url` in a generated skill's manifest.

    Checks `workbench/skills/<id>/manifest.json` first. For skills generated
    before B4 landed (manifest lacks start_url), falls back to the noui
    backend's workflow-sessions endpoint using the recorded session_id.
    Returns ``(url, source)`` where ``source`` is ``"manifest"``, ``"backend"``,
    or ``""`` on miss.
    """
    manifest_path = _find_skill_manifest(skill_id)
    if not (manifest_path and manifest_path.exists()):
        return ("", "")

    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception:
        return ("", "")

    workflow = manifest.get("workflow") or {}
    url = workflow.get("start_url") or ""
    if url:
        return (url, "manifest")

    session_id = workflow.get("workflow_session_id", "")
    if not session_id:
        return ("", "")
    try:
        import urllib.request

        with urllib.request.urlopen(
            f"{BACKEND_URL}/workflow-sessions/{session_id}", timeout=5
        ) as r:
            data = json.loads(r.read().decode())
        url = data.get("start_url", "")
        if url:
            return (url, "backend")
    except Exception:
        pass
    return ("", "")


def _print_worker_log_tail(log_path: Path, start_offset: int, *, n: int = 30) -> None:
    """Print the last ``n`` non-blank lines of the worker log to stderr.

    ``start_offset`` scopes the tail to lines written since the current
    `session ensure` invocation began, so we don't leak noise from previous
    crashes. Falls back to the whole-file tail when the bounded read is
    empty (e.g. the worker died before writing anything new).
    """
    try:
        if log_path.exists():
            size = log_path.stat().st_size
            if size > start_offset:
                with log_path.open("rb") as fh:
                    fh.seek(start_offset)
                    chunk = fh.read().decode("utf-8", errors="replace")
                lines = [ln.rstrip() for ln in chunk.splitlines() if ln.strip()][-n:]
                if lines:
                    print(f"  Worker logs: {log_path}", file=sys.stderr)
                    print(f"  Last {len(lines)} lines since this ensure started:", file=sys.stderr)
                    for line in lines:
                        print(f"    {line}", file=sys.stderr)
                    return
        # Fallback: whole-file tail (covers the case where the offset logic
        # can't find anything new, e.g. file truncated).
        lines = _tail_file_lines(log_path, n)
        if lines:
            print(f"  Worker logs: {log_path}", file=sys.stderr)
            print(f"  Last {len(lines)} lines:", file=sys.stderr)
            for line in lines:
                print(f"    {line}", file=sys.stderr)
        else:
            print(f"  Worker logs: {log_path} (empty)", file=sys.stderr)
    except Exception as exc:
        # Never let log-tail failures mask the real error.
        print(f"  (could not read worker log tail: {exc})", file=sys.stderr)


def _tail_file_lines(path: Path, n: int) -> list[str]:
    """Return the last ``n`` non-blank lines of ``path``, oldest first.

    Robust against missing files and decode errors. Used by session-ensure's
    timeout branch and by the log-streaming tailer for retrospective context.
    """
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return lines[-n:]


def _spawn_worker_log_tailer(
    log_path: Path, fatal_event: threading.Event, start_offset: int
) -> threading.Thread:
    """Start a daemon thread that tails ``log_path`` from byte ``start_offset``.

    Echoes progress lines directly; on a fatal pattern match, prints the line
    and sets ``fatal_event`` so the main poll loop can exit early instead of
    waiting for the full 5-minute health-check timeout.

    The tailer is a best-effort aid: it runs until the event is set or the
    process exits. It does not guarantee delivery of every line (file reads
    are bounded to 64 KiB per tick) and does not block the main loop.
    """

    def _run() -> None:
        offset = start_offset
        while not fatal_event.is_set():
            try:
                if not log_path.exists():
                    time.sleep(0.5)
                    continue
                size = log_path.stat().st_size
                if size < offset:
                    # File was truncated / rotated; reset.
                    offset = 0
                if size <= offset:
                    time.sleep(0.5)
                    continue
                with log_path.open("rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read(min(size - offset, 65536))
                offset += len(chunk)
                try:
                    text = chunk.decode("utf-8", errors="replace")
                except Exception:
                    continue
                for raw_line in text.splitlines():
                    line = raw_line.rstrip()
                    if not line:
                        continue
                    if any(pat in line for pat in _WORKER_FATAL_PATTERNS):
                        # Newline to break out of the "progress dots" row, then the line.
                        print()
                        print(_red(f"  [worker] {line[:400]}"))
                        fatal_event.set()
                        return
                    if any(pat in line for pat in _WORKER_PROGRESS_PATTERNS):
                        print()
                        print(_cyan(f"  [worker] {line[:200]}"))
            except Exception:
                # Never let the tailer itself take down the command.
                time.sleep(1)

    thread = threading.Thread(target=_run, name="tabby-worker-log-tailer", daemon=True)
    thread.start()
    return thread


def _mark_session_terminated(session_id: str) -> None:
    sql = f"UPDATE sessions SET state='TERMINATED' WHERE id='{session_id}'"
    subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "browser_hitl",
            "-d",
            "browser_hitl",
            "-c",
            sql,
        ],
        cwd=str(TABBY_DIR),
        capture_output=True,
    )


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
    except urllib.error.URLError as exc:
        # Connection refused, DNS failure, timeout, etc. The underlying OSError
        # (or socket.timeout) is exposed via exc.reason; surface a clear,
        # user-actionable message instead of letting the traceback escape.
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            detail = f"timed out after {timeout}s"
        else:
            detail = str(reason) or type(reason).__name__
        raise RuntimeError(
            f"Cannot reach Tabby at {url} ({detail}). Is Tabby running? Try `noui tabby status`."
        ) from exc


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
            color = str  # type: ignore[assignment]
        print(f"  {_cyan(sid[:8])}  {_bold(app)}  {color(status)}  {url}")

    print()
    return 0


def cmd_login_export(args: argparse.Namespace) -> int:
    """Analyze session and write bundle JSON to workbench/login_recordings/."""
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
        # Use dom_check instead of url_check — url_check is fragile (429, redirects)
        patched_draft["keepalive_config"] = {
            "interval_seconds": 300,
            "actions": [],
            "health_checks": [{"type": "dom_check", "selector": "body", "exists": True}],
            "policy": "all",
        }
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

    # Add profile to Tabby cache so session ensure can find it
    credential_ref = (
        bundle.get("application_draft", {})
        .get("login_config", {})
        .get("credential_ref", f"k8s:secret/tabby-{profile_id}")
    )
    cache = _load_cache()
    apps = cache.setdefault("apps", {})
    entry = apps.setdefault(profile_id, {})
    entry["app_id"] = app_id
    entry["profile_db_id"] = profile_db_id
    entry["credential_ref"] = credential_ref
    defaults = cache.setdefault("default_profiles", [])
    if profile_id not in defaults:
        defaults.append(profile_id)
    _save_cache(cache)

    print()
    print(_green(f"Registered profile '{profile_id}'"))
    print(f"  Application ID       : {_cyan(app_id)}")
    print(f"  ServiceProfile DB ID : {_cyan(profile_db_id)}")
    print(f"  Tabby profile ID     : {_cyan(profile_id)}")
    print("  Version state        : STAGING")
    print()
    print("  Next steps:")
    print(f"    {_bold(f'noui login credentials {bundle_path}')}")
    return 0


def cmd_login_validate(args: argparse.Namespace) -> int:
    """Wait for a HEALTHY browser session to exist for the registered profile's app."""
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
    app_id = provisioned.get("app_id", "")
    profile_id = provisioned.get("profile_id", profile_db_id)
    if not profile_db_id or not app_id:
        print(_red("No profile_db_id/app_id found in bundle — run `noui login register` first"))
        return 1

    admin_token = _get_admin_token()
    if not admin_token:
        return 1

    print(
        f"Polling sessions for app {_cyan(app_id)} (profile '{profile_id}') up to 60s",
        end="",
        flush=True,
    )
    deadline = time.time() + 60
    last_error: Exception | None = None
    while time.time() < deadline:
        time.sleep(3)
        print(".", end="", flush=True)
        try:
            sessions = _get_sessions(admin_token, raise_on_error=True)
            healthy = [
                s for s in sessions if s.get("app_id") == app_id and s.get("state") == "HEALTHY"
            ]
            if healthy:
                print()
                print(
                    _green(
                        f"✓ Session for profile '{profile_id}' is HEALTHY (session {healthy[0].get('id', '')[:8]}…)"
                    )
                )
                return 0
            failed = [
                s
                for s in sessions
                if s.get("app_id") == app_id and s.get("state") in ("FAILED", "ERROR")
            ]
            if failed:
                print()
                print(_red(f"Session entered failed state: {failed[0].get('state')}"))
                return 1
        except (RuntimeError, AssertionError) as exc:
            # Don't abort polling on a single failed call (Tabby may be briefly
            # restarting). Remember the last error so we can surface it if we
            # eventually time out instead of swallowing it silently.
            last_error = exc
    else:
        print()
        if last_error is not None:
            print(
                _red(
                    f"No HEALTHY session found within 60s. Last polling error: {last_error}. "
                    f"Run: noui tabby session ensure"
                )
            )
        else:
            print(_red("No HEALTHY session found within 60s — run: noui tabby session ensure"))
        return 1


def cmd_login_credentials(args: argparse.Namespace) -> int:
    """Set username and password for a registered login profile."""
    bundle_path = Path(args.bundle_file)
    if not bundle_path.exists():
        print(_red(f"Bundle file not found: {bundle_path}"))
        return 1

    try:
        bundle = json.loads(bundle_path.read_text())
    except Exception as exc:
        print(_red(f"Failed to parse bundle: {exc}"))
        return 1

    provisioned = bundle.get("_provisioned")
    if not provisioned:
        print(_red("Bundle has not been registered yet. Run: noui login register"))
        return 1

    profile_id: str = provisioned["profile_id"]
    profile_db_id: str = provisioned["profile_db_id"]
    app_id: str = provisioned["app_id"]
    credential_ref: str = (
        bundle.get("application_draft", {})
        .get("login_config", {})
        .get("credential_ref", f"k8s:secret/tabby-{profile_id}")
    )
    secret = credential_ref.replace("k8s:secret/", "")
    prefix = _env_prefix(secret)

    print(f"Setting credentials for profile '{_bold(profile_id)}'")
    print()

    username = input("  Username (email): ").strip()
    if not username:
        print(_red("  Username cannot be empty."))
        return 1

    password = getpass.getpass("  Password: ")
    if not password:
        print(_red("  Password cannot be empty."))
        return 1

    # Update cache with username and credential_ref
    cache = _load_cache()
    apps = cache.setdefault("apps", {})
    entry = apps.setdefault(profile_id, {})
    entry["app_id"] = app_id
    entry["profile_db_id"] = profile_db_id
    entry["username"] = username
    entry["credential_ref"] = credential_ref

    # Add to default_profiles if not already there
    defaults = cache.setdefault("default_profiles", [])
    if profile_id not in defaults:
        defaults.append(profile_id)

    _save_cache(cache)

    # Write password to .env.local
    _write_env_vars(ENV_LOCAL, {f"{prefix}_PASSWORD": password})

    print()
    print(_green("Credentials saved."))
    print(f"  Cache   : {TABBY_CREDS_CACHE}")
    print(f"  Env file: {ENV_LOCAL}")
    print()
    print("  Next: start a browser session:")
    print(f"    {_bold(f'noui tabby session ensure --profile {profile_id}')}")
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
    print(f"  Then run: {_bold(f'noui workflow export {session_id} --profile <tabby_profile_id>')}")
    print("           (defaults to --as both; pass --as mcp or --as skill to pick one)")
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
            color = str  # type: ignore[assignment]
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
            color = str  # type: ignore[assignment]
        print(
            f"  {_cyan(sid[:8])}  {_cyan(sid)}  {color(status)}  project:{project_id[:8] if project_id else '—'}"
        )

    print()
    return 0


def cmd_workflow_export(args: argparse.Namespace) -> int:
    """Compile workflow session to an MCP server, a Skill, or both."""
    if not _backend_alive():
        print(_red(f"NoUI backend not reachable at {BACKEND_URL}"))
        return 1

    session_id: str = args.session_id
    target: str = getattr(args, "target", "both")
    profile_id: str = getattr(args, "profile", "")
    profile_slug: str = getattr(args, "profile_slug", "")
    profile_db_id: str = getattr(args, "profile_db_id", "")
    capture_session_id: str = getattr(args, "capture_session", "")
    description_override: str = getattr(args, "description_override", "")
    execution_mode: str = getattr(args, "execution_mode", "cdp")
    do_verify: bool = getattr(args, "verify", False)

    if target not in ("mcp", "skill", "both"):
        print(_red(f"Invalid --as {target!r}. Expected 'mcp', 'skill', or 'both'."))
        return 2

    from urllib.parse import quote_plus

    params: list[str] = [f"as={target}", f"execution_mode={execution_mode}"]
    if profile_id:
        params.append(f"tabby_profile_id={profile_id}")
    if profile_slug:
        params.append(f"profile_slug={profile_slug}")
    if profile_db_id:
        params.append(f"profile_db_id={profile_db_id}")
    if capture_session_id:
        params.append(f"capture_session_id={capture_session_id}")
    if description_override:
        params.append(f"description_override={quote_plus(description_override)}")
    path = f"/workflow-sessions/{session_id}/export?" + "&".join(params)

    print(
        f"Exporting workflow {_cyan(session_id)} as {_bold(target)} …",
        end=" ",
        flush=True,
    )
    try:
        result = _http("POST", path)
        assert isinstance(result, dict)
        print(_green("done"))
    except (RuntimeError, AssertionError) as exc:
        print()
        print(_red(f"Export failed: {exc}"))
        return 1

    mcp_manifest = result.get("mcp") or {}
    skill_manifest = result.get("skill") or {}

    server_id = mcp_manifest.get("server_id", "")
    skill_id = skill_manifest.get("skill_id", "")

    print()
    if mcp_manifest:
        tool_count = len(mcp_manifest.get("tools", []))
        print(_bold("MCP server generated:"))
        print(f"  Server ID  : {_cyan(server_id or '?')}")
        print(f"  Tools      : {tool_count}")
        auth_info = mcp_manifest.get("auth", {})
        if auth_info.get("requires_auth"):
            strategy = auth_info.get("strategy") or "tabby_credentials"
            slug = auth_info.get("profile_slug") or auth_info.get("tabby_profile_id") or ""
            print(f"  Auth       : {strategy} (profile: {slug or '?'})")
            if not slug:
                print()
                print(_yellow("  ⚠  This server requires auth but no profile was linked."))
                print(
                    _yellow(
                        f"     Re-export with: noui workflow export {session_id} --as mcp --profile-slug <slug>"
                    )
                )
                print(_yellow("     Available profiles: noui tabby session status"))
        else:
            print("  Auth       : none (public API)")
        print()

    if skill_manifest:
        op_count = len(skill_manifest.get("operations", []))
        print(_bold("Skill generated:"))
        print(f"  Skill ID   : {_cyan(skill_id or '?')}")
        print(f"  Operations : {op_count}")
        auth_info = skill_manifest.get("auth", {})
        if auth_info.get("requires_auth"):
            strategy = auth_info.get("strategy") or "tabby_credentials"
            slug = auth_info.get("profile_slug") or ""
            print(f"  Auth       : {strategy} (profile: {slug or '?'})")
        else:
            print("  Auth       : none (public API)")
        print()

    if do_verify and server_id:
        print(f"Running auth verification for {_cyan(server_id)} …")
        rc = _run_mcp_verify(server_id)
        if rc != 0:
            return rc

    if server_id:
        print(f"  Install MCP  : {_bold(f'noui mcp install {server_id} claude-code')}")
    if skill_id:
        print(
            f"  Install Skill: {_bold(f'noui skill install {skill_id} <agent>')}"
            "  (agent: claude-code | codex | cline | opencode | agents; add --project for project scope)"
        )
    return 0


def _run_mcp_verify(server_id: str) -> int:
    """Run auth verification for a compiled MCP server."""
    import asyncio

    manifest_path = _find_mcp_manifest(server_id)
    if not manifest_path:
        print(_red(f"  Server {server_id!r} not found in workbench/mcp_servers/"))
        return 1

    server_dir = manifest_path.parent
    auth_plan_path = server_dir / "auth_plan.json"
    if not auth_plan_path.exists():
        print(_green("  No auth_plan.json — server is public, no verification needed."))
        return 0

    try:
        sys.path.insert(0, str(NOUI_DIR))
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
        print(_red("  Auth verification FAILED — missing secrets:"))
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
# autopilot subcommands
# ---------------------------------------------------------------------------


def cmd_autopilot_start_capture(args: argparse.Namespace) -> int:
    """Create workflow + capture sessions and start HAR/click recording."""
    if not _backend_alive():
        print(_red(f"NoUI backend not reachable at {BACKEND_URL}"))
        return 1

    name = args.name
    url = args.url

    print(f"Creating workflow session '{name}' …", end=" ", flush=True)
    try:
        wf = _http(
            "POST", "/workflow-sessions", {"name": name, "start_url": url, "description": name}
        )
    except RuntimeError as exc:
        print(_red(f"\nFailed: {exc}"))
        return 1
    assert isinstance(wf, dict)
    wf_id = wf["id"]
    process_id = wf["process_id"]
    project_id = wf["project_id"]
    print(_green("done"))

    # Start workflow
    try:
        _http("POST", f"/workflow-sessions/{wf_id}/start")
    except RuntimeError as exc:
        print(_red(f"Failed to start workflow: {exc}"))
        return 1

    # Create capture session
    try:
        cs = _http(
            "POST",
            f"/processes/{process_id}/capture-sessions",
            {
                "click_tracking": True,
                "url_monitoring": True,
                "har_capture": True,
            },
        )
    except RuntimeError as exc:
        print(_red(f"Failed to create capture session: {exc}"))
        return 1
    assert isinstance(cs, dict)
    cs_id = cs["id"]

    # Start capture session (PUT)
    try:
        req = urllib.request.Request(
            f"{BACKEND_URL}/capture-sessions/{cs_id}/start",
            data=b"{}",
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            json.loads(resp.read().decode())
    except Exception as exc:
        print(_red(f"Failed to start capture: {exc}"))
        return 1

    # Start HAR + click tracking via extension (best effort)
    har_started = False
    try:
        result = _http(
            "POST",
            "/browser-commands/execute",
            {
                "command_type": "start_capture_session",
                "params": {
                    "projectId": project_id,
                    "processId": process_id,
                    "captureSessionId": cs_id,
                },
            },
            timeout=10,
        )
        # The execute endpoint returns HTTP 200 even on extension errors —
        # check the success field to detect failures.
        if isinstance(result, dict) and result.get("success") is False:
            raise RuntimeError(result.get("error", "Extension command failed"))
        har_started = True
    except Exception:
        # Fallback: try individual commands
        try:
            _http(
                "POST",
                "/browser-commands/execute",
                {
                    "command_type": "set_capture_state",
                    "params": {
                        "projectId": project_id,
                        "processId": process_id,
                        "captureSessionId": cs_id,
                    },
                },
                timeout=10,
            )
            _http(
                "POST",
                "/browser-commands/execute",
                {
                    "command_type": "start_har_capture",
                    "params": {"captureSessionId": cs_id},
                },
                timeout=10,
            )
            _http(
                "POST",
                "/browser-commands/execute",
                {
                    "command_type": "inject_click_tracker",
                    "params": {},
                },
                timeout=10,
            )
            _http(
                "POST",
                "/browser-commands/execute",
                {
                    "command_type": "start_url_monitoring",
                    "params": {
                        "projectId": project_id,
                        "processId": process_id,
                        "captureSessionId": cs_id,
                    },
                },
                timeout=10,
            )
            har_started = True
        except Exception as exc:
            print(_yellow(f"  Extension not responding — HAR capture not started: {exc}"))

    print()
    print(_bold("Capture started:"))
    print(f"  Workflow session  : {_cyan(wf_id)}")
    print(f"  Capture session   : {_cyan(cs_id)}")
    print(f"  Project ID        : {project_id}")
    print(f"  Process ID        : {process_id}")
    if har_started:
        print(f"  HAR capture       : {_green('active')}")
    else:
        print(f"  HAR capture       : {_yellow('not started (extension not connected)')}")
    print()
    print("  Now drive the browser with:")
    print(f"    {_bold(f'noui autopilot browser navigate url={url}')}")
    print(f"    {_bold('noui autopilot browser query_elements selector=...')}")
    print(f"    {_bold('noui autopilot browser type_text selector=... text=...')}")
    print(f"    {_bold('noui autopilot browser click_element selector=...')}")
    print()
    print("  When done:")
    print(f"    {_bold(f'noui autopilot stop-capture {wf_id} {cs_id}')}")
    return 0


def cmd_autopilot_stop_capture(args: argparse.Namespace) -> int:
    """Stop capture, complete workflow, wait for HAR upload."""
    if not _backend_alive():
        print(_red(f"NoUI backend not reachable at {BACKEND_URL}"))
        return 1

    wf_id = args.workflow_session_id
    cs_id = args.capture_session_id

    # Stop extension capture (HAR upload happens here)
    print("Stopping extension capture …", end=" ", flush=True)
    try:
        _http(
            "POST",
            "/browser-commands/execute",
            {
                "command_type": "stop_capture_session",
                "params": {"captureSessionId": cs_id},
            },
            timeout=35,
        )
        print(_green("done"))
    except Exception:
        print(_yellow("skipped (extension not responding)"))

    # Stop capture session in DB (PUT)
    print("Stopping capture session …", end=" ", flush=True)
    try:
        req = urllib.request.Request(
            f"{BACKEND_URL}/capture-sessions/{cs_id}/stop",
            data=b"{}",
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            json.loads(resp.read().decode())
        print(_green("done"))
    except Exception as exc:
        print(_yellow(f"skipped ({exc})"))

    # Complete workflow session
    print("Completing workflow session …", end=" ", flush=True)
    try:
        _http("POST", f"/workflow-sessions/{wf_id}/complete")
        print(_green("done"))
    except RuntimeError as exc:
        print(_yellow(f"skipped ({exc})"))

    # Wait for HAR upload
    print("Waiting for HAR upload …", end=" ", flush=True)
    time.sleep(3)

    # Check if HAR exists
    has_har = False
    try:
        req = urllib.request.Request(
            f"{BACKEND_URL}/capture-sessions/{cs_id}/har",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            har_size = len(resp.read())
            has_har = har_size > 0
    except Exception:
        pass

    if has_har:
        print(_green(f"done ({har_size:,} bytes)"))
    else:
        print(_yellow("no HAR file found"))

    print()
    if has_har:
        print(_green("Capture complete.") + " Next:")
        print(f"    {_bold(f'noui autopilot export {wf_id} {cs_id}')}")
    else:
        print(_yellow("No HAR captured.") + " The extension may not have been recording.")
        print("  You can still try to export (may fail if no API calls were captured):")
        print(f"    {_bold(f'noui autopilot export {wf_id} {cs_id}')}")
    return 0


def cmd_autopilot_export(args: argparse.Namespace) -> int:
    """Validate HAR and export MCP server."""
    if not _backend_alive():
        print(_red(f"NoUI backend not reachable at {BACKEND_URL}"))
        return 1

    wf_id = args.workflow_session_id
    cs_id = args.capture_session_id
    profile_slug = getattr(args, "profile_slug", "")
    execution_mode = getattr(args, "execution_mode", "cdp")

    params = [f"capture_session_id={cs_id}", "as=mcp", f"execution_mode={execution_mode}"]
    if profile_slug:
        params.append(f"profile_slug={profile_slug}")
    path = f"/workflow-sessions/{wf_id}/export?" + "&".join(params)

    print(f"Exporting workflow {_cyan(wf_id)} to MCP …", end=" ", flush=True)
    try:
        result = _http("POST", path)
        assert isinstance(result, dict)
        print(_green("done"))
    except (RuntimeError, AssertionError) as exc:
        print()
        print(_red(f"Export failed: {exc}"))
        return 1

    mcp_manifest = result.get("mcp") or {}
    server_id = mcp_manifest.get("server_id", "?")
    tool_count = len(mcp_manifest.get("tools", []))

    print()
    print(_bold("MCP server generated:"))
    print(f"  Server ID  : {_cyan(server_id)}")
    print(f"  Tools      : {tool_count}")
    auth_info = mcp_manifest.get("auth", {})
    if auth_info.get("requires_auth"):
        strategy = auth_info.get("strategy") or "tabby_credentials"
        slug = auth_info.get("profile_slug") or "?"
        print(f"  Auth       : {strategy} (profile: {slug})")
    else:
        print("  Auth       : none (public API)")
    print()
    print(f"  Install: {_bold(f'noui mcp install {server_id} claude-code')}")
    return 0


def cmd_autopilot_browser(args: argparse.Namespace) -> int:
    """Execute a browser command via the extension."""
    if not _backend_alive():
        print(_red(f"NoUI backend not reachable at {BACKEND_URL}"))
        return 1

    cmd_type = args.browser_command
    raw_args = args.browser_args

    # Build params dict from positional args
    # Supports: key=value pairs or a single positional value for common commands
    params: dict = {}
    if raw_args:
        for arg in raw_args:
            if "=" in arg:
                key, value = arg.split("=", 1)
                params[key] = value
            else:
                # For convenience: single arg maps to the primary param of common commands
                _primary_param = {
                    "navigate": "url",
                    "click_element": "selector",
                    "click_at": "x",
                    "click_by_text": "text",
                    "type_text": "selector",
                    "type_into_label": "label",
                    "wait_for_selector": "selector",
                    "wait_for_url": "url_substring",
                    "press_key": "key",
                    "select_option": "selector",
                }
                primary = _primary_param.get(cmd_type)
                if primary and primary not in params:
                    params[primary] = arg
                else:
                    # For type_into_label: first arg=label, second arg=text
                    if (
                        cmd_type == "type_into_label"
                        and "label" in params
                        and "text" not in params
                        or cmd_type == "type_text"
                        and "selector" in params
                        and "text" not in params
                    ):
                        params["text"] = arg
                    elif (
                        cmd_type == "select_option"
                        and "selector" in params
                        and "value" not in params
                    ):
                        params["value"] = arg
                    elif cmd_type == "click_at" and "x" in params and "y" not in params:
                        params["y"] = arg

    # Convert types for specific commands
    if cmd_type == "click_at":
        params["x"] = float(params.get("x", 0))
        params["y"] = float(params.get("y", 0))
    if cmd_type == "click_by_text" and "exact" in params:
        params["exact"] = str(params["exact"]).lower() in ("true", "1", "yes")

    try:
        result = _http(
            "POST",
            "/browser-commands/execute",
            {
                "command_type": cmd_type,
                "params": params,
            },
            timeout=35,
        )
        print(json.dumps(result, indent=2))
        return 0
    except RuntimeError as exc:
        print(_red(f"Browser command failed: {exc}"))
        return 1


def cmd_autopilot_verify_extension(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Pre-flight check: verify the Chrome extension supports all expected browser commands."""
    if not _backend_alive():
        print(_red(f"NoUI backend not reachable at {BACKEND_URL}"))
        return 1

    # Commands to test — a representative set covering original + agent-friendly commands
    test_commands = [
        ("get_page_info", {}),
        ("get_page_summary", {}),
        ("press_key", {"key": "Shift"}),  # harmless no-op key
        ("query_elements", {"selector": "body"}),
    ]

    print(_bold("Verifying extension commands …"))
    print()
    all_ok = True
    for cmd_type, params in test_commands:
        print(f"  {cmd_type:<25}", end="", flush=True)
        try:
            result = _http(
                "POST",
                "/browser-commands/execute",
                {"command_type": cmd_type, "params": params},
                timeout=10,
            )
            assert isinstance(result, dict)
            if result.get("error"):
                # Command reached extension but returned an error — still means the
                # command type is recognised, which is what we care about.
                if "Unknown command type" in str(result["error"]):
                    print(_red("UNSUPPORTED"))
                    all_ok = False
                else:
                    print(_green("ok"))
            elif result.get("success") is False and "Unknown command type" in str(
                result.get("error", "")
            ):
                print(_red("UNSUPPORTED"))
                all_ok = False
            else:
                print(_green("ok"))
        except Exception as exc:
            err = str(exc)
            if "Unknown command type" in err:
                print(_red("UNSUPPORTED"))
            else:
                print(_red(f"FAILED ({err[:60]})"))
            all_ok = False

    print()
    if all_ok:
        print(_green("All commands supported.") + " Extension is ready for autopilot.")
    else:
        print(
            _red("Some commands are missing.")
            + " Reload the extension in chrome://extensions and retry."
        )
    return 0 if all_ok else 1


def cmd_autopilot_capture_status(args: argparse.Namespace) -> int:
    """Show live status of a capture session — whether it's recording and if HAR exists."""
    if not _backend_alive():
        print(_red(f"NoUI backend not reachable at {BACKEND_URL}"))
        return 1

    cs_id = args.capture_session_id

    try:
        cs = _http("GET", f"/capture-sessions/{cs_id}")
        assert isinstance(cs, dict)
    except (RuntimeError, AssertionError) as exc:
        print(_red(f"Failed to get capture session: {exc}"))
        return 1

    status = cs.get("status", "unknown")
    status_color = {
        "capturing": _green,
        "stopped": _yellow,
        "idle": _cyan,
        "paused": _yellow,
    }.get(status, _red)

    print(_bold(f"Capture Session {cs_id}"))
    print()
    print(f"  Status          : {status_color(status)}")
    print(f"  HAR capture     : {'enabled' if cs.get('har_capture') else 'disabled'}")
    print(f"  Click tracking  : {'enabled' if cs.get('click_tracking') else 'disabled'}")
    print(f"  URL monitoring  : {'enabled' if cs.get('url_monitoring') else 'disabled'}")
    print(f"  Started at      : {cs.get('started_at') or 'not started'}")
    print(f"  Stopped at      : {cs.get('stopped_at') or 'still running'}")

    # Check if HAR file exists
    has_har = False
    har_size = 0
    if cs.get("har_file_path"):
        print(f"  HAR file        : {_green('present')}")
        has_har = True
    else:
        try:
            req = urllib.request.Request(
                f"{BACKEND_URL}/capture-sessions/{cs_id}/har",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                har_size = len(resp.read())
                has_har = har_size > 0
        except Exception:
            pass
        if has_har:
            print(f"  HAR file        : {_green(f'present ({har_size:,} bytes)')}")
        else:
            print(f"  HAR file        : {_yellow('not found')}")

    return 0


def cmd_autopilot_resume_capture(args: argparse.Namespace) -> int:
    """Create a new capture session on an existing workflow, restarting HAR recording."""
    if not _backend_alive():
        print(_red(f"NoUI backend not reachable at {BACKEND_URL}"))
        return 1

    wf_id = args.workflow_session_id

    # Get workflow session to find process_id and project_id
    try:
        wf = _http("GET", f"/workflow-sessions/{wf_id}")
        assert isinstance(wf, dict)
    except (RuntimeError, AssertionError) as exc:
        print(_red(f"Failed to get workflow session: {exc}"))
        return 1

    process_id = wf.get("process_id")
    project_id = wf.get("project_id")
    if not process_id or not project_id:
        print(_red("Workflow session missing process_id or project_id"))
        return 1

    wf_status = wf.get("status", "")
    print(f"Workflow session: {_cyan(wf_id)} (status: {wf_status})")

    # Restart workflow if it was completed
    if wf_status == "completed":
        print("Re-opening completed workflow …", end=" ", flush=True)
        try:
            _http("POST", f"/workflow-sessions/{wf_id}/start")
            print(_green("done"))
        except RuntimeError as exc:
            print(_yellow(f"skipped ({exc})"))

    # Create new capture session
    print("Creating new capture session …", end=" ", flush=True)
    try:
        cs = _http(
            "POST",
            f"/processes/{process_id}/capture-sessions",
            {
                "click_tracking": True,
                "url_monitoring": True,
                "har_capture": True,
            },
        )
    except RuntimeError as exc:
        print(_red(f"\nFailed: {exc}"))
        return 1
    assert isinstance(cs, dict)
    cs_id = cs["id"]
    print(_green("done"))

    # Start capture session (PUT)
    try:
        req = urllib.request.Request(
            f"{BACKEND_URL}/capture-sessions/{cs_id}/start",
            data=b"{}",
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            json.loads(resp.read().decode())
    except Exception as exc:
        print(_red(f"Failed to start capture: {exc}"))
        return 1

    # Start HAR + click tracking via extension
    har_started = False
    try:
        result = _http(
            "POST",
            "/browser-commands/execute",
            {
                "command_type": "start_capture_session",
                "params": {
                    "projectId": project_id,
                    "processId": process_id,
                    "captureSessionId": cs_id,
                },
            },
            timeout=10,
        )
        if isinstance(result, dict) and result.get("success") is False:
            raise RuntimeError(result.get("error", "Extension command failed"))
        har_started = True
    except Exception:
        try:
            _http(
                "POST",
                "/browser-commands/execute",
                {
                    "command_type": "set_capture_state",
                    "params": {
                        "projectId": project_id,
                        "processId": process_id,
                        "captureSessionId": cs_id,
                    },
                },
                timeout=10,
            )
            _http(
                "POST",
                "/browser-commands/execute",
                {"command_type": "start_har_capture", "params": {"captureSessionId": cs_id}},
                timeout=10,
            )
            _http(
                "POST",
                "/browser-commands/execute",
                {"command_type": "inject_click_tracker", "params": {}},
                timeout=10,
            )
            _http(
                "POST",
                "/browser-commands/execute",
                {
                    "command_type": "start_url_monitoring",
                    "params": {
                        "projectId": project_id,
                        "processId": process_id,
                        "captureSessionId": cs_id,
                    },
                },
                timeout=10,
            )
            har_started = True
        except Exception as exc:
            print(_yellow(f"  Extension not responding — HAR capture not started: {exc}"))

    print()
    print(_bold("Capture resumed:"))
    print(f"  Workflow session  : {_cyan(wf_id)}")
    print(f"  Capture session   : {_cyan(cs_id)} (new)")
    print(f"  Project ID        : {project_id}")
    print(f"  Process ID        : {process_id}")
    if har_started:
        print(f"  HAR capture       : {_green('active')}")
    else:
        print(f"  HAR capture       : {_yellow('not started (extension not connected)')}")
    print()
    print("  When done:")
    print(f"    {_bold(f'noui autopilot stop-capture {wf_id} {cs_id}')}")
    return 0


def cmd_autopilot_list(args: argparse.Namespace) -> int:  # noqa: ARG001
    """List autopilot recording runs."""
    if not _backend_alive():
        print(_red(f"NoUI backend not reachable at {BACKEND_URL}"))
        return 1

    try:
        runs = _http("GET", "/autopilot-recordings")
        assert isinstance(runs, list)
    except (RuntimeError, AssertionError) as exc:
        print(_red(f"Failed to list runs: {exc}"))
        return 1

    if not runs:
        print("No autopilot recording runs found.")
        return 0

    print(f"{'ID':<38} {'Status':<20} {'Website':<40} {'Tools':<6} {'Created'}")
    print("-" * 120)
    for r in runs:
        run_id = r.get("id", "?")[:36]
        status = r.get("status", "?")
        website = r.get("website_url", "?")[:38]
        tools = str(r.get("tools_count", 0))
        created = r.get("created_at", "?")[:19]
        print(f"{run_id:<38} {status:<20} {website:<40} {tools:<6} {created}")

    return 0


def cmd_autopilot_status(args: argparse.Namespace) -> int:
    """Show status of an autopilot run."""
    if not _backend_alive():
        print(_red(f"NoUI backend not reachable at {BACKEND_URL}"))
        return 1

    run_id = args.run_id
    try:
        result = _http("GET", f"/autopilot-recordings/{run_id}")
        assert isinstance(result, dict)
    except (RuntimeError, AssertionError) as exc:
        print(_red(f"Failed to get run: {exc}"))
        return 1

    print(_bold(f"Autopilot Run {run_id}"))
    print()
    for key in [
        "status",
        "website_url",
        "login_url",
        "task_description",
        "success_condition",
        "stop_condition",
        "tabby_profile_id",
        "workflow_session_id",
        "capture_session_id",
        "server_id",
        "mcp_output_path",
        "tools_count",
        "failure_reason",
        "created_at",
        "updated_at",
        "completed_at",
    ]:
        val = result.get(key, "")
        if val:
            label = key.replace("_", " ").title()
            print(f"  {label:<25}: {val}")

    return 0


# ---------------------------------------------------------------------------
# mcp subcommands
# ---------------------------------------------------------------------------


def _find_mcp_manifest(server_id: str) -> Path | None:
    """Search workbench/mcp_servers/ for a manifest.json matching server_id."""
    if not MCP_SERVERS_DIR.exists():
        return None
    # Direct path: workbench/mcp_servers/<server_id>/manifest.json
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
        print(_yellow("No MCP servers found (workbench/mcp_servers/ does not exist)."))
        return 0

    manifests = list(MCP_SERVERS_DIR.rglob("manifest.json"))
    if not manifests:
        print(_yellow("No MCP server manifests found in workbench/mcp_servers/."))
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

    # Check CDP accessibility for servers that use the browser-via-CDP pattern
    ops_dir = manifest_path.parent / "operations"
    uses_cdp = (
        any(
            "CDP_LIST_URL" in op_file.read_text(encoding="utf-8", errors="ignore")
            for op_file in ops_dir.glob("*.py")
        )
        if ops_dir.exists()
        else False
    )
    if uses_cdp:
        if _cdp_is_reachable():
            print(f"  CDP       : {_green('reachable (localhost:9222)')}")
        else:
            print(f"  CDP       : {_red('not reachable (localhost:9222)')}")
            print(_yellow("             Run: noui tabby session ensure"))

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
        print(
            _yellow(
                f"'{server_id}' is already configured in Claude Desktop. Use --force to overwrite."
            )
        )
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
        print(
            _yellow(
                f"'{server_id}' is already configured in Claude Code. Use --force to overwrite."
            )
        )
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
        print(
            _yellow(f"'{server_id}' is already configured in Codex CLI. Use --force to overwrite.")
        )
        return 0

    new_block = (
        f'\n[mcp_servers.{server_id}]\ncommand = "{python_cmd}"\nargs = ["{server_script}"]\n'
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
        print(
            _yellow(f"'{server_id}' is already configured in OpenCode. Use --force to overwrite.")
        )
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


def cmd_mcp_docs(args: argparse.Namespace) -> int:
    """Regenerate API.md for a generated MCP server from its current tools.json."""
    server_id: str = args.server_id
    check_only: bool = getattr(args, "check", False)

    manifest_path = _find_mcp_manifest(server_id)
    if manifest_path is None:
        print(_red(f"MCP server '{server_id}' not found in {MCP_SERVERS_DIR}"))
        return 1

    server_dir = manifest_path.parent

    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as exc:
        print(_red(f"Failed to parse manifest.json: {exc}"))
        return 1

    tools_path = server_dir / "tools.json"
    if not tools_path.exists():
        print(_red(f"tools.json not found in {server_dir} — cannot regenerate API.md"))
        return 1

    try:
        tool_defs = json.loads(tools_path.read_text())
    except Exception as exc:
        print(_red(f"Failed to parse tools.json: {exc}"))
        return 1

    # Resolve identity fields from manifest
    app_info = manifest.get("app", {})
    app_name = app_info.get("name", _slug_to_title(server_id))
    app_slug = app_info.get("slug", server_id)
    workflow_info = manifest.get("workflow", {})
    workflow_name = workflow_info.get("name", app_name)
    auth_info = manifest.get("auth", {})
    tabby_profile_id = auth_info.get("tabby_profile_id") or ""

    # Import the generator (keeps it optional — CLI works even if compiler not on PYTHONPATH)
    try:
        sys.path.insert(0, str(NOUI_DIR))
        from compiler.mcp.api_doc_generator import generate_api_markdown
    except ImportError as exc:
        print(_red(f"Cannot import api_doc_generator: {exc}"))
        return 1

    new_content = generate_api_markdown(
        server_id=server_id,
        app_name=app_name,
        app_slug=app_slug,
        workflow_name=workflow_name,
        tool_defs=tool_defs,
        tabby_profile_id=tabby_profile_id,
    )

    api_md_path = server_dir / "API.md"

    if check_only:
        if api_md_path.exists():
            existing = api_md_path.read_text(encoding="utf-8")

            # Strip the generated-at timestamp line before comparing (it always differs)
            def _strip_timestamp(text: str) -> str:
                return "\n".join(
                    line
                    for line in text.splitlines()
                    if not line.startswith("> **Generated by NoUI** on ")
                )

            if _strip_timestamp(existing) == _strip_timestamp(new_content):
                print(_green(f"API.md is up to date: {api_md_path}"))
                return 0
            else:
                print(_red(f"API.md is stale — run 'noui mcp docs {server_id}' to refresh"))
                return 1
        else:
            print(_red(f"API.md does not exist — run 'noui mcp docs {server_id}' to generate"))
            return 1

    api_md_path.write_text(new_content, encoding="utf-8")
    print(_green(f"API.md written: {api_md_path}"))

    # Ensure manifest artifacts lists API.md
    artifacts = manifest.setdefault("artifacts", {})
    artifacts["api_docs_file"] = "API.md"
    files_list: list = artifacts.get("files", [])
    if "API.md" not in files_list:
        files_list.append("API.md")
        artifacts["files"] = files_list
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    return 0


def _slug_to_title(slug: str) -> str:
    import re as _re

    return " ".join(word.capitalize() for word in _re.split(r"[-_]+", slug))


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
        print(_red(f"Server {server_id!r} not found in workbench/mcp_servers/"))
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
        print("  fallbacks      :")
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
        sys.path.insert(0, str(NOUI_DIR))
        from compiler.mcp.auth_verifier import verify_before_install

        result = asyncio.run(verify_before_install(server_dir))
    except Exception as exc:
        print(_red(f"Verification error: {exc}"))
        return 1

    status_color = (
        _green
        if result.status == "PASS"
        else (_yellow if result.status == "REPAIR_APPLIED" else _red)
    )
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
# skill subcommands
# ---------------------------------------------------------------------------


def _find_skill_manifest(skill_id: str) -> Path | None:
    """Search workbench/skills/ for a manifest.json matching skill_id."""
    if not SKILLS_DIR.exists():
        return None
    direct = SKILLS_DIR / skill_id / "manifest.json"
    if direct.exists():
        return direct
    for manifest_path in SKILLS_DIR.rglob("manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text())
            if manifest.get("skill_id") == skill_id:
                return manifest_path
        except Exception:
            continue
    return None


SKILL_AGENTS = ("claude-code", "codex", "cline", "opencode", "agents")


def _skill_install_root(agent: str, project: bool) -> Path:
    """Return the skills directory for a given agent + scope.

    Sources: Claude Code docs, Codex skills docs, Cline skills docs, OpenCode
    docs. The `agents` target writes to the shared .agents/skills/ convention
    honored by Codex, OpenCode, and the Vercel Labs npx skills ecosystem —
    functionally equivalent to `codex` on disk, exposed under a second name
    for users who'd rather not name Codex explicitly.

    Path.cwd() / Path.home() are resolved at call time so tests (and any
    caller that chdirs) get the current working directory.
    """
    if agent == "claude-code":
        return Path.cwd() / ".claude" / "skills" if project else Path.home() / ".claude" / "skills"
    if agent in ("codex", "agents"):
        return Path.cwd() / ".agents" / "skills" if project else Path.home() / ".agents" / "skills"
    if agent == "cline":
        return Path.cwd() / ".cline" / "skills" if project else Path.home() / ".cline" / "skills"
    if agent == "opencode":
        return (
            Path.cwd() / ".opencode" / "skills"
            if project
            else Path.home() / ".config" / "opencode" / "skills"
        )
    raise ValueError(f"Unknown agent {agent!r}; expected one of {SKILL_AGENTS}")


def cmd_skill_list(args: argparse.Namespace) -> int:  # noqa: ARG001
    """List generated skills under workbench/skills/."""
    if not SKILLS_DIR.exists():
        print(_yellow("No skills found (workbench/skills/ does not exist)."))
        return 0

    manifests = list(SKILLS_DIR.rglob("manifest.json"))
    # Filter to skill manifests (schema_version 1 + runtime.type claude-code-skill)
    skill_manifests: list[tuple[Path, dict]] = []
    for mp in manifests:
        try:
            m = json.loads(mp.read_text())
        except Exception:
            continue
        if m.get("runtime", {}).get("type") == "claude-code-skill":
            skill_manifests.append((mp, m))

    if not skill_manifests:
        print(_yellow("No skill manifests found in workbench/skills/."))
        return 0

    print(_bold("Skills:"))
    print()
    header = f"  {'SKILL ID':<32} {'APP':<24} {'OPS':>4}  AUTH"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for _manifest_path, manifest in sorted(skill_manifests, key=lambda x: x[1].get("skill_id", "")):
        skill_id = manifest.get("skill_id", "?")
        app = manifest.get("app", {}).get("name", "?")
        ops = len(manifest.get("operations", []))
        auth = manifest.get("auth", {})
        auth_str = (
            f"{auth.get('strategy', 'tabby_credentials')} ({auth.get('profile_slug') or '?'})"
            if auth.get("requires_auth")
            else "public"
        )
        print(f"  {_cyan(skill_id):<32} {app:<24} {ops:>4}  {auth_str}")
    print()
    return 0


def cmd_skill_show(args: argparse.Namespace) -> int:
    """Print a skill's manifest and SKILL.md preview.

    With --smoke, also invoke each operation with `--help` to catch import
    errors / missing deps before agent invocation time.
    """
    skill_id: str = args.skill_id
    smoke: bool = getattr(args, "smoke", False)

    manifest_path = _find_skill_manifest(skill_id)
    if not manifest_path:
        print(_red(f"Skill {skill_id!r} not found in {SKILLS_DIR}"))
        return 1

    skill_dir = manifest_path.parent
    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as exc:
        print(_red(f"Failed to read manifest: {exc}"))
        return 1

    print(_bold(f"Skill: {_cyan(skill_id)}"))
    print(f"  Path       : {skill_dir}")
    print(f"  App        : {manifest.get('app', {}).get('name', '?')}")
    print(f"  Operations : {len(manifest.get('operations', []))}")
    auth = manifest.get("auth", {})
    if auth.get("requires_auth"):
        print(f"  Auth       : {auth.get('strategy')} (profile: {auth.get('profile_slug')})")
    else:
        print("  Auth       : none (public API)")
    print()

    skill_md = skill_dir / "SKILL.md"
    if skill_md.exists():
        body = skill_md.read_text(encoding="utf-8")
        # Show frontmatter + first 20 lines of body
        try:
            fm_end = body.index("\n---\n", 4) + 5
        except ValueError:
            fm_end = 0
        print(_bold("SKILL.md (frontmatter + preview):"))
        head = body[:fm_end] + "\n".join(body[fm_end:].splitlines()[:20])
        print(head)

    if smoke:
        return _smoke_test_skill(manifest, skill_dir)
    return 0


def _smoke_test_skill(manifest: dict, skill_dir: Path) -> int:
    """Run `python <op> --help` for each operation and report pass/fail.

    Uses manifest.runtime.python_executable (resolved under skill_dir) when
    present, falling back to `sys.executable`. A failing `--help` means the
    operation can't even import — Claude Code won't be able to invoke it
    either.
    """
    import subprocess

    runtime = manifest.get("runtime", {}) or {}
    python_rel = runtime.get("python_executable", "")
    python_exe: str
    if python_rel:
        python_path = skill_dir / python_rel
        python_exe = str(python_path) if python_path.exists() else sys.executable
        if not python_path.exists():
            print(
                _yellow(
                    f"  runtime.python_executable {python_rel!r} not found; falling back to {sys.executable}"
                )
            )
    else:
        python_exe = sys.executable

    operations = manifest.get("operations", []) or []
    if not operations:
        print(_yellow("No operations to smoke-test."))
        return 0

    print()
    print(_bold("Smoke test (--help per operation):"))
    print()
    width = max(len(op.get("name", "?")) for op in operations) + 2
    any_failed = False
    for op in operations:
        name = op.get("name", "?")
        module_rel = op.get("module", f"operations/{name}.py")
        module_path = skill_dir / module_rel
        if not module_path.exists():
            print(f"  {_red('FAIL'):10} {name:<{width}}  module file missing: {module_rel}")
            any_failed = True
            continue
        try:
            completed = subprocess.run(
                [python_exe, str(module_path), "--help"],
                cwd=str(skill_dir),
                capture_output=True,
                text=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            print(f"  {_red('TIMEOUT'):10} {name:<{width}}  --help did not return within 5s")
            any_failed = True
            continue
        except Exception as exc:
            print(f"  {_red('ERROR'):10} {name:<{width}}  {exc}")
            any_failed = True
            continue
        if completed.returncode == 0:
            print(f"  {_green('ok'):10} {name:<{width}}")
        else:
            err_first = (completed.stderr or completed.stdout or "").splitlines()
            err_snippet = err_first[-1] if err_first else f"exit {completed.returncode}"
            print(f"  {_red('FAIL'):10} {name:<{width}}  {err_snippet[:120]}")
            any_failed = True
    print()
    if any_failed:
        print(_red("One or more operations failed smoke test."))
        return 1
    print(_green("All operations passed smoke test."))
    return 0


def cmd_skill_docs(args: argparse.Namespace) -> int:
    """Regenerate SKILL.md + API.md for a skill, preserving custom-fenced regions.

    Mirrors `cmd_mcp_docs` for the skill output format. Reads the current
    `manifest.json` and re-emits docs using the operation list, auth plan, and
    identity fields from the manifest. Custom-fenced regions (``<!-- custom:
    start:NAME --> … <!-- custom:end:NAME -->``) in the existing SKILL.md /
    API.md are carried forward verbatim.
    """
    skill_id: str = args.skill_id
    check_only: bool = getattr(args, "check", False)

    manifest_path = _find_skill_manifest(skill_id)
    if manifest_path is None:
        print(_red(f"Skill {skill_id!r} not found in {SKILLS_DIR}"))
        return 1
    skill_dir = manifest_path.parent

    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as exc:
        print(_red(f"Failed to parse manifest.json: {exc}"))
        return 1

    app_info = manifest.get("app", {}) or {}
    app_name = app_info.get("name", _slug_to_title(skill_id))
    app_slug = app_info.get("slug", skill_id)
    workflow_info = manifest.get("workflow", {}) or {}
    workflow_name = workflow_info.get("name", app_name)
    auth_info = manifest.get("auth", {}) or {}
    profile_slug = auth_info.get("profile_slug") or ""
    runtime = manifest.get("runtime", {}) or {}
    python_executable = runtime.get("python_executable", ".venv/bin/python")

    # Reconstruct tool_defs from manifest.operations. Note: operations[].args
    # loses some richness (no request body shape) vs HAR-derived tool_defs; the
    # renderer tolerates missing fields and omits sections that would be empty.
    tool_defs: list[dict] = []
    for op in manifest.get("operations", []) or []:
        tool_defs.append(
            {
                "name": op.get("name", "?"),
                "description": op.get("description", op.get("name", "?")),
                "method": op.get("method", "?"),
                "path": op.get("path", "?"),
                "params": [
                    {
                        "name": a.get("name", "?"),
                        "type": a.get("type", "string"),
                        "required": bool(a.get("required", False)),
                        **({"default": a["default"]} if "default" in a else {}),
                    }
                    for a in op.get("args", []) or []
                ],
            }
        )

    # Load the auth_plan.json if referenced, so description-synth knows the profile context.
    auth_plan: dict = {}
    auth_plan_file = auth_info.get("auth_plan_file")
    if auth_plan_file:
        auth_plan_path = skill_dir / auth_plan_file
        if auth_plan_path.exists():
            try:
                auth_plan = json.loads(auth_plan_path.read_text())
            except Exception:
                auth_plan = {}
    # Treat any requires_auth=True as having auth_plan for rendering purposes
    if auth_info.get("requires_auth") and not auth_plan:
        auth_plan = {"strategy": auth_info.get("strategy") or "tabby_credentials"}

    try:
        sys.path.insert(0, str(NOUI_DIR))
        from compiler.mcp.api_doc_generator import generate_api_markdown
        from compiler.skill.skill_md_generator import render_skill_md
    except ImportError as exc:
        print(_red(f"Cannot import skill doc generators: {exc}"))
        return 1

    skill_md_path = skill_dir / "SKILL.md"
    api_md_path = skill_dir / "API.md"

    existing_skill_md = (
        skill_md_path.read_text(encoding="utf-8") if skill_md_path.exists() else None
    )
    existing_api_md = api_md_path.read_text(encoding="utf-8") if api_md_path.exists() else None

    new_skill_md = render_skill_md(
        skill_id=skill_id,
        app_name=app_name,
        app_slug=app_slug,
        workflow_name=workflow_name,
        tool_defs=tool_defs,
        auth_plan=auth_plan,
        profile_slug=profile_slug,
        python_executable=python_executable,
        existing=existing_skill_md,
    )
    new_api_md = generate_api_markdown(
        server_id=skill_id,
        app_name=app_name,
        app_slug=app_slug,
        workflow_name=workflow_name,
        tool_defs=tool_defs,
        tabby_profile_id=profile_slug,
        existing=existing_api_md,
    )

    def _strip_timestamp(text: str) -> str:
        return "\n".join(
            line for line in text.splitlines() if not line.startswith("> **Generated by NoUI** on ")
        )

    if check_only:
        stale = False
        for label, existing, fresh in (
            ("SKILL.md", existing_skill_md, new_skill_md),
            ("API.md", existing_api_md, new_api_md),
        ):
            if existing is None:
                print(
                    _red(f"{label} does not exist — run `noui skill docs {skill_id}` to generate")
                )
                stale = True
                continue
            if _strip_timestamp(existing) != _strip_timestamp(fresh):
                print(_red(f"{label} is stale — run `noui skill docs {skill_id}` to refresh"))
                stale = True
            else:
                print(_green(f"{label} is up to date"))
        return 1 if stale else 0

    skill_md_path.write_text(new_skill_md, encoding="utf-8")
    api_md_path.write_text(new_api_md, encoding="utf-8")
    print(_green(f"SKILL.md written: {skill_md_path}"))
    print(_green(f"API.md written: {api_md_path}"))

    # Keep manifest artifacts.files in sync (ensure both doc files listed).
    artifacts = manifest.setdefault("artifacts", {})
    artifacts.setdefault("skill_file", "SKILL.md")
    artifacts.setdefault("api_docs_file", "API.md")
    files_list: list = artifacts.get("files", []) or []
    for required in ("SKILL.md", "API.md"):
        if required not in files_list:
            files_list.append(required)
    artifacts["files"] = files_list
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


def cmd_skill_install(args: argparse.Namespace) -> int:
    """Copy (or symlink) a generated skill into the target agent's skills directory.

    Flags:
        --symlink: link the install path to the workbench source instead of
            copying. Edits in either location are visible from the other.
            Fails gracefully on platforms without symlink support.
        --with-env: provision a Python virtualenv inside the install dir so
            the skill's operations have their declared deps. Values: auto|uv|
            venv|none. Default none.
    """
    import os
    import shutil

    skill_id: str = args.skill_id
    agent: str = args.agent
    project: bool = getattr(args, "project", False)
    symlink: bool = getattr(args, "symlink", False)
    with_env: str = getattr(args, "with_env", "none") or "none"

    manifest_path = _find_skill_manifest(skill_id)
    if not manifest_path:
        print(_red(f"Skill {skill_id!r} not found in {SKILLS_DIR}"))
        return 1

    src = manifest_path.parent
    try:
        dest_root = _skill_install_root(agent, project)
    except ValueError as exc:
        print(_red(str(exc)))
        return 2
    dest = dest_root / skill_id

    dest_root.mkdir(parents=True, exist_ok=True)

    # Handle an existing install. Only rmtree a regular dir; if it's already a
    # symlink, just unlink it (don't follow into workbench and delete files).
    if dest.is_symlink():
        dest.unlink()
    elif dest.exists():
        shutil.rmtree(dest)

    if symlink:
        try:
            os.symlink(src.resolve(), dest, target_is_directory=True)
        except OSError as exc:
            print(
                _red(
                    f"Symlink failed ({exc}). On Windows this typically needs developer mode or admin. "
                    f"Retry without --symlink to use copy mode."
                )
            )
            return 3
    else:
        shutil.copytree(src, dest)

    scope = "project" if project else "global"
    link_mode = "symlink" if symlink else "copy"
    print(_green(f"Installed {_cyan(skill_id)} for {_bold(agent)} ({scope}, {link_mode})"))
    print(f"  {dest}")

    # --with-env provisioning (D2). Skip by default in symlink mode so we don't
    # create a .venv in the workbench source; the user can opt in explicitly.
    if with_env != "none":
        env_dir = dest if not symlink else src
        rc = _provision_skill_env(env_dir, mode=with_env)
        if rc != 0:
            return rc

    return 0


def _provision_skill_env(skill_dir: Path, *, mode: str) -> int:
    """Run `uv sync` or `python -m venv + pip install -e .` inside `skill_dir`.

    `mode` is one of auto | uv | venv | none. `none` is a no-op.
    `auto` picks uv when available, else venv, else prints the manual command.
    """
    import shutil
    import subprocess

    if mode == "none":
        return 0

    uv_path = shutil.which("uv")

    if mode == "uv" and not uv_path:
        print(_red("--with-env uv requested but `uv` is not on PATH."))
        return 4

    if mode == "auto" and not uv_path:
        # Fall back to venv if python is available, else bail with a hint.
        mode = "venv"

    if mode in ("uv", "auto") and uv_path:
        print(_bold(f"Provisioning environment via `uv sync` in {skill_dir} …"))
        rc = subprocess.call([uv_path, "sync"], cwd=str(skill_dir))
        if rc != 0:
            print(_red(f"`uv sync` failed with exit code {rc}."))
            return rc
        print(_green("Environment ready."))
        return 0

    # venv fallback
    python_cmd = shutil.which("python3") or shutil.which("python")
    if not python_cmd:
        print(
            _red(
                "Neither `uv` nor `python` is on PATH. Run one of the following inside "
                f"{skill_dir}:\n  uv sync\n  python -m venv .venv && .venv/bin/pip install -e ."
            )
        )
        return 5

    print(_bold(f"Provisioning environment via venv+pip in {skill_dir} …"))
    venv_rc = subprocess.call([python_cmd, "-m", "venv", ".venv"], cwd=str(skill_dir))
    if venv_rc != 0:
        print(_red(f"venv creation failed with exit code {venv_rc}."))
        return venv_rc
    pip_path = str(skill_dir / ".venv" / "bin" / "pip")
    pip_rc = subprocess.call([pip_path, "install", "-e", "."], cwd=str(skill_dir))
    if pip_rc != 0:
        print(_red(f"`pip install -e .` failed with exit code {pip_rc}."))
        return pip_rc
    print(_green("Environment ready."))
    return 0


def cmd_skill_uninstall(args: argparse.Namespace) -> int:
    """Remove an installed skill from the target agent's skills directory."""
    import shutil

    skill_id: str = args.skill_id
    agent: str = args.agent
    project: bool = getattr(args, "project", False)

    try:
        dest = _skill_install_root(agent, project) / skill_id
    except ValueError as exc:
        print(_red(str(exc)))
        return 2
    if not dest.exists():
        print(_yellow(f"{skill_id!r} is not installed for {agent} at {dest}"))
        return 0
    shutil.rmtree(dest)
    print(_green(f"Uninstalled {_cyan(skill_id)} from {_bold(agent)}"))
    print(f"  {dest}")
    return 0


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
            (
                p
                for p in profiles
                if p.get("profile_id") == profile_id and p.get("version_state") == "ACTIVE"
            ),
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
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "browser_hitl",
                "-d",
                "browser_hitl",
                "-c",
                sql,
            ],
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
            {
                "action": "fill",
                "selector": cfg["pass_sel"],
                "value": "${PASSWORD}",
                "sensitive": True,
            },
            {"action": "click", "selector": cfg["submit_sel"]},
        ]
        if cfg.get("otp_required") and cfg.get("otp_sel"):
            steps += [
                {
                    "action": "wait_for",
                    "selector": cfg["otp_sel"],
                    "timeout_ms": 120000,
                    "sensitive": True,
                },
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

        entry.update(
            {
                "app_id": app_id,
                "login_url": cfg["login_url"],
                "username": cfg.get("username", ""),
                "credential_ref": app_payload["login_config"]["credential_ref"],
                "login_config": app_payload["login_config"],
            }
        )
        if cfg.get("username") and cfg.get("password"):
            secret = _secret_name(profile_id)
            prefix = _env_prefix(secret)
            _write_env_vars(
                ENV_LOCAL,
                {
                    f"{prefix}_USERNAME": cfg["username"],
                    f"{prefix}_PASSWORD": cfg["password"],
                },
            )

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


def _get_sessions(admin_token: str, *, raise_on_error: bool = False) -> list[dict[str, Any]]:
    """Fetch all browser sessions known to Tabby.

    By default, transport / HTTP failures are swallowed and an empty list is
    returned so that status-style callers can show "no sessions" without
    aborting. Pass ``raise_on_error=True`` when the caller needs to distinguish
    "Tabby is unreachable" from "Tabby returned zero sessions" (for example,
    inside a polling loop that should surface persistent failures on timeout).
    """
    try:
        resp = _tabby_http("GET", "/sessions?limit=200", token=admin_token)
        if isinstance(resp, dict):
            return resp.get("data", [])
        return list(resp)  # type: ignore[arg-type]
    except RuntimeError:
        if raise_on_error:
            raise
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

    print(_bold("Tabby worker build:"))
    build_state, detail = _tabby_worker_build_state()
    if build_state == "ok":
        print(_green(f"  ✓  {detail}"))
    elif build_state == "missing":
        print(_red(f"  ✗  {detail}"))
        print(f"     Fix: {_bold(_tabby_worker_build_hint())}")
    elif build_state == "stale":
        print(_yellow(f"  !  {detail}"))
        print(f"     Rebuild: {_bold(_tabby_worker_build_hint())}")
    else:
        print(_yellow(f"  !  {detail}"))
    print()

    print(_bold("Tabby API:"))
    if _tabby_alive():
        pid = _read_pid(TABBY_PID_FILE)
        pid_label = f" (PID {pid})" if pid else ""
        print(_green(f"  ✓  API ready at {TABBY_API_HOST}{pid_label}"))
        return 0 if build_state == "ok" else 1
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
            print(
                _yellow("AGENT_SECRET_HMAC_KEY, ADMIN_BOOTSTRAP_EMAIL, ADMIN_BOOTSTRAP_PASSWORD.")
            )
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
    print(
        f"Starting Tabby API (PID {proc.pid}) … logs → {_cyan(str(TABBY_LOG_FILE))}",
        end="",
        flush=True,
    )

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
    build_state, detail = _tabby_worker_build_state()
    if build_state in ("missing", "stale"):
        print(_red(f"Tabby worker is not ready: {detail}"))
        print(f"  Run this first: {_bold(_tabby_worker_build_hint())}")
        print("  Then retry `noui tabby setup`. We refuse to proceed because the worker")
        print(
            "  would crash on every `tabby session ensure`, failing silently after a 5-minute wait."
        )
        return 1
    if build_state == "no-worker":
        print(_red(f"Tabby source tree missing: {detail}"))
        print("  Did the submodule init? Try: `git submodule update --init --recursive`.")
        return 1

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
        print(
            _red(f"ADMIN_BOOTSTRAP_EMAIL and ADMIN_BOOTSTRAP_PASSWORD must be set in {ENV_LOCAL}")
        )
        return 1

    print(f"Logging in as {_cyan(admin_email)} …", end=" ", flush=True)
    try:
        login_resp = _tabby_http(
            "POST", "/login", {"email": admin_email, "password": admin_password}
        )
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
            existing_clients = _tabby_http(
                "GET", f"/admin/agent-clients/{tenant_id}", token=admin_token
            )
            if not isinstance(existing_clients, list):
                existing_clients = []
        except RuntimeError:
            existing_clients = []

        match = next(
            (c for c in existing_clients if c.get("name") == TABBY_AGENT_CLIENT_NAME), None
        )

        if match and not args.force:
            print(
                f"Agent client '{TABBY_AGENT_CLIENT_NAME}' already exists — rotating secret …",
                end=" ",
                flush=True,
            )
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

    cache.update(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "default_profiles": allowed_profiles,
        }
    )
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
    _write_env_vars(
        env_file,
        {
            "TABBY_API_URL": TABBY_API_HOST,
            "TABBY_CLIENT_ID": client_id,
            "TABBY_CLIENT_SECRET": client_secret,
        },
    )

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
    build_state, detail = _tabby_worker_build_state()
    if build_state in ("missing", "stale"):
        print(_red(f"Tabby worker is not ready: {detail}"))
        print(f"  Run: {_bold(_tabby_worker_build_hint())}")
        print("  `tabby session ensure` would otherwise hang up to 5 minutes waiting for a")
        print("  worker that never successfully starts. Fix the build first.")
        return 1
    if build_state == "no-worker":
        print(_red(f"Tabby source tree missing: {detail}"))
        print("  Run: `git submodule update --init --recursive`")
        return 1

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
            print(
                _red("Could not determine profile. Run 'noui tabby setup' first or pass --profile.")
            )
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
        pid = _read_pid(TABBY_WORKER_PID_FILE)
        if pid and _pid_running(pid) and _cdp_is_reachable():
            print(_green(f"✓ Session for '{profile_id}' is already HEALTHY"))
            # Still honor --open / --skill against the existing session.
            _maybe_navigate_from_args(args)
            return 0
        # Worker has died but DB still shows HEALTHY — clear the stale state
        _mark_session_terminated(healthy[0]["id"])
        _clear_pid(TABBY_WORKER_PID_FILE)
        print(_yellow("  Stale HEALTHY session detected (worker not running) — restarting …"))

    print(f"No HEALTHY session for '{_cyan(profile_id)}' — starting one …")

    session_id = _seed_session(app_id, tenant_id)
    if not session_id:
        return 1
    print(f"  Session ID: {_cyan(session_id)}")

    env = {**os.environ}
    env.update(_load_env_local())
    env.update(
        {
            "SESSION_ID": session_id,
            "APP_ID": app_id,
            "TENANT_ID": tenant_id,
            "STREAMING_MODE": "cdp",
        }
    )

    creds_mount = Path("/tmp/tabby-local-secrets")
    secret_name = entry.get("credential_ref", "k8s:secret/no-auth").replace("k8s:secret/", "")
    secret_dir = creds_mount / secret_name
    secret_dir.mkdir(parents=True, exist_ok=True)
    if entry.get("username"):
        env_local_vars = _load_env_local()
        prefix = _env_prefix(secret_name)
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
    # Record the log's current size so the tailer only shows lines from this
    # worker invocation, not stale lines from a previous crash.
    log_start_offset = TABBY_WORKER_LOG_FILE.stat().st_size if TABBY_WORKER_LOG_FILE.exists() else 0
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

    # B2: stream the log alongside the health-check poll. A fatal pattern in
    # the tailer sets this event so we exit early instead of waiting 5 minutes.
    fatal_event = threading.Event()
    _spawn_worker_log_tailer(TABBY_WORKER_LOG_FILE, fatal_event, log_start_offset)

    print()
    print("  Waiting for health check to pass", end="", flush=True)

    final_state = ""
    final_health = ""
    timed_out = False
    for _ in range(60):
        time.sleep(5)
        if fatal_event.is_set():
            # The tailer already printed the offending line.
            print(_red("  Worker emitted a fatal log line — aborting health-check wait."))
            print(f"  Worker logs: {TABBY_WORKER_LOG_FILE}")
            return 1
        print(".", end="", flush=True)
        # Worker process died entirely? No point waiting further.
        if proc.poll() is not None:
            print()
            print(
                _red(f"Worker process exited with code {proc.returncode} before becoming HEALTHY.")
            )
            _print_worker_log_tail(TABBY_WORKER_LOG_FILE, log_start_offset, n=30)
            return 1
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
                _print_worker_log_tail(TABBY_WORKER_LOG_FILE, log_start_offset, n=30)
                return 1
        except (RuntimeError, AssertionError):
            pass
    else:
        timed_out = True

    if timed_out:
        print()
        print(_red("Session did not pass health check within 5 minutes."))
        _print_worker_log_tail(TABBY_WORKER_LOG_FILE, log_start_offset, n=30)
        return 1

    # Signal the tailer to stop — we're past the health-check gate.
    fatal_event.set()

    print()

    if final_state != "HEALTHY":
        print("  Promoting session state to HEALTHY …", end=" ", flush=True)
        sql = f"UPDATE sessions SET state='HEALTHY' WHERE id='{session_id}'"
        try:
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    "postgres",
                    "psql",
                    "-U",
                    "browser_hitl",
                    "-d",
                    "browser_hitl",
                    "-c",
                    sql,
                ],
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

    _maybe_navigate_from_args(args)

    print()
    print("  You can now record a workflow with Tabby auth:")
    _cmd = 'noui workflow record "My Workflow" <url>'
    print(f"    {_bold(_cmd)}")
    return 0


def _maybe_navigate_from_args(args: argparse.Namespace) -> None:
    """If --open or --skill was passed, navigate the CDP tab to the target URL.

    Silent no-op when neither is passed. On failure, prints a warning but does
    not fail the command — the session is HEALTHY regardless; users can
    navigate manually via the CDP relay at localhost:9222 if this best-effort
    navigation doesn't work.
    """
    target_url: str = getattr(args, "open_url", None) or ""
    skill_arg: str = getattr(args, "open_skill", None) or ""
    if skill_arg and not target_url:
        target_url, source = _skill_manifest_start_url(skill_arg)
        if not target_url:
            print(
                _yellow(
                    f"  Skill '{skill_arg}' has no start_url — pass --open URL directly. "
                    "Skills generated before B4 won't carry start_url in manifest; "
                    "re-export to populate it."
                )
            )
            return
        print(f"  Resolved --skill {skill_arg} start_url from {source}: {target_url}")
    if not target_url:
        return
    print(f"  Navigating browser to {_cyan(target_url)} …", end=" ", flush=True)
    ok, detail = _navigate_cdp_page(target_url)
    if ok:
        print(_green("✓"))
    else:
        print(_yellow("⚠"))
        print(
            _yellow(
                f"  Navigation failed ({detail}). Session is still HEALTHY; you can "
                "navigate manually via the CDP relay at localhost:9222."
            )
        )


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
        "export", help="Analyze session and write bundle JSON to workbench/login_recordings/"
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

    login_credentials = login_sub.add_parser(
        "credentials", help="Set username/password for a registered profile"
    )
    login_credentials.add_argument("bundle_file", help="Path to bundle JSON file")

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

    wf_export = wf_sub.add_parser("export", help="Compile workflow session to MCP, Skill, or both")
    wf_export.add_argument("session_id", help="Workflow session ID")
    wf_export.add_argument(
        "--as",
        dest="target",
        default="both",
        choices=["mcp", "skill", "both"],
        help="Output format (default: both)",
    )
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
        help="Tabby profile slug for runtime credential requests (e.g. 'example-bank')",
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
        "--description-override",
        default="",
        metavar="TEXT",
        dest="description_override",
        help="Skill only: explicit SKILL.md description (skip the heuristic)",
    )
    wf_export.add_argument(
        "--verify",
        action="store_true",
        default=False,
        help="Run auth verification after MCP export; report PASS/NEEDS_SECRET before install",
    )
    wf_export.add_argument(
        "--execution-mode",
        default="cdp",
        choices=["cdp", "http"],
        dest="execution_mode",
        help=(
            "Execution strategy: 'cdp' (default, runs inside Tabby's browser) "
            "or 'http' (legacy httpx + resolve_auth)"
        ),
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
    mcp_install_p.add_argument(
        "--force", action="store_true", help="Overwrite existing configuration"
    )

    mcp_docs_p = mcp_sub.add_parser("docs", help="Regenerate API.md from current tools.json")
    mcp_docs_p.add_argument("server_id", help="MCP server ID")
    mcp_docs_p.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if API.md is stale without regenerating",
    )

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

    # --- skill ---
    skill_parser = sub.add_parser("skill", help="Generated skill commands")
    skill_sub = skill_parser.add_subparsers(dest="skill_command")

    skill_sub.add_parser("list", help="List generated skills")

    skill_show_p = skill_sub.add_parser("show", help="Print skill manifest + SKILL.md preview")
    skill_show_p.add_argument("skill_id", help="Skill ID")
    skill_show_p.add_argument(
        "--smoke",
        action="store_true",
        help="Run each operation with --help as a smoke test; fail fast on import errors / missing deps",
    )

    skill_install_p = skill_sub.add_parser(
        "install", help="Install skill into a specific agent's skills directory"
    )
    skill_install_p.add_argument("skill_id", help="Skill ID")
    skill_install_p.add_argument(
        "agent",
        choices=list(SKILL_AGENTS),
        help=(
            "Target agent. `agents` is the shared .agents/skills/ convention "
            "(same on-disk path as `codex`) for users who'd rather pick the "
            "cross-agent convention explicitly."
        ),
    )
    skill_install_p.add_argument(
        "--project",
        action="store_true",
        help="Install to the project-scoped path for this agent (e.g. .claude/skills/, .agents/skills/) instead of the global path",
    )
    skill_install_p.add_argument(
        "--symlink",
        action="store_true",
        help="Symlink the install path to the workbench source for in-place dev iteration (edits propagate both ways)",
    )
    skill_install_p.add_argument(
        "--with-env",
        choices=("auto", "uv", "venv", "none"),
        default="none",
        help=(
            "Provision a Python virtualenv inside the installed skill so its deps are available. "
            "`auto` tries uv, falls back to venv. In --symlink mode the env lands in the workbench source."
        ),
    )

    skill_docs_p = skill_sub.add_parser(
        "docs", help="Regenerate SKILL.md + API.md from manifest, preserving custom-fenced regions"
    )
    skill_docs_p.add_argument("skill_id", help="Skill ID")
    skill_docs_p.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if docs differ from regeneration; do not overwrite",
    )

    skill_uninstall_p = skill_sub.add_parser("uninstall", help="Uninstall a skill")
    skill_uninstall_p.add_argument("skill_id", help="Skill ID")
    skill_uninstall_p.add_argument(
        "agent",
        choices=list(SKILL_AGENTS),
        help="Agent whose skills directory to remove from",
    )
    skill_uninstall_p.add_argument(
        "--project",
        action="store_true",
        help="Remove from the project-scoped path instead of the global path",
    )

    # --- autopilot ---
    ap_parser = sub.add_parser("autopilot", help="Autopilot recording commands")
    ap_sub = ap_parser.add_subparsers(dest="autopilot_command")

    ap_sub.add_parser("list", help="List autopilot recording runs")

    ap_status = ap_sub.add_parser("status", help="Show status of an autopilot run")
    ap_status.add_argument("run_id", help="Autopilot run ID")

    ap_sub.add_parser(
        "verify-extension", help="Pre-flight check: verify extension supports all browser commands"
    )

    ap_capstatus = ap_sub.add_parser("capture-status", help="Show live status of a capture session")
    ap_capstatus.add_argument("capture_session_id", help="Capture session ID")

    ap_resume = ap_sub.add_parser(
        "resume-capture",
        help="Create a new capture session on an existing workflow (after broken capture)",
    )
    ap_resume.add_argument("workflow_session_id", help="Workflow session ID to resume")

    # Capture lifecycle
    ap_start = ap_sub.add_parser(
        "start-capture", help="Create sessions and start HAR + click capture"
    )
    ap_start.add_argument("name", help="Workflow name")
    ap_start.add_argument("url", help="Website start URL")

    ap_stop = ap_sub.add_parser(
        "stop-capture", help="Stop capture, complete workflow, wait for HAR upload"
    )
    ap_stop.add_argument("workflow_session_id", help="Workflow session ID (from start-capture)")
    ap_stop.add_argument("capture_session_id", help="Capture session ID (from start-capture)")

    ap_export = ap_sub.add_parser("export", help="Validate HAR and export MCP server")
    ap_export.add_argument("workflow_session_id", help="Workflow session ID")
    ap_export.add_argument("capture_session_id", help="Capture session ID")
    ap_export.add_argument("--profile-slug", default="", help="Tabby profile slug for auth")
    ap_export.add_argument(
        "--execution-mode",
        default="cdp",
        choices=["cdp", "http"],
        dest="execution_mode",
        help=(
            "Execution strategy: 'cdp' (default, runs inside Tabby's browser) "
            "or 'http' (legacy httpx + resolve_auth)"
        ),
    )

    # Browser command passthrough — lets Claude Code drive the browser from the CLI
    ap_browser = ap_sub.add_parser("browser", help="Execute a browser command via the extension")
    ap_browser.add_argument(
        "browser_command", help="Command type (e.g. get_page_info, click_element, navigate)"
    )
    ap_browser.add_argument(
        "browser_args",
        nargs="*",
        default=[],
        help="Command arguments as key=value pairs or positional values",
    )

    # --- tabby ---
    tabby_parser = sub.add_parser("tabby", help="Tabby credential service lifecycle commands")
    tabby_sub = tabby_parser.add_subparsers(dest="tabby_command")

    tabby_sub.add_parser("status", help="Check Docker Compose services and Tabby API liveness")
    tabby_sub.add_parser("start", help="Start Docker Compose infra and Tabby API in background")

    tabby_stop_p = tabby_sub.add_parser("stop", help="Stop the Tabby API process")
    tabby_stop_p.add_argument(
        "--infra", action="store_true", help="Also stop Docker Compose services"
    )

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
    ensure_p.add_argument(
        "--open",
        dest="open_url",
        metavar="URL",
        default=None,
        help=(
            "After the session is HEALTHY, navigate the browser tab to URL so the site's "
            "session cookies provision before any subsequent tool call"
        ),
    )
    ensure_p.add_argument(
        "--skill",
        dest="open_skill",
        metavar="SKILL_ID",
        default=None,
        help=(
            "Shortcut: pull the start URL from a generated skill's manifest "
            "(workflow.start_url) instead of passing --open directly"
        ),
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
        print("Usage: noui login {record,list,export,review,register,validate,credentials,import}")
        return 1
    dispatch = {
        "record": cmd_login_record,
        "list": cmd_login_list,
        "export": cmd_login_export,
        "review": cmd_login_review,
        "register": cmd_login_register,
        "validate": cmd_login_validate,
        "credentials": cmd_login_credentials,
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
        print("Usage: noui workflow {record,list,captures,export}")
        return 1
    dispatch = {
        "record": cmd_workflow_record,
        "list": cmd_workflow_list,
        "captures": cmd_workflow_captures,
        "export": cmd_workflow_export,
    }
    fn = dispatch.get(cmd)
    if fn is None:
        print(_red(f"Unknown workflow subcommand: {cmd}"))
        return 1
    return fn(args)


def _dispatch_mcp(args: argparse.Namespace) -> int:
    cmd = getattr(args, "mcp_command", None)
    if cmd is None:
        print("Usage: noui mcp {list,status,start,stop,install,docs,verify,diagnose-auth}")
        return 1
    dispatch = {
        "list": cmd_mcp_list,
        "status": cmd_mcp_status,
        "start": cmd_mcp_start,
        "stop": cmd_mcp_stop,
        "install": cmd_mcp_install,
        "docs": cmd_mcp_docs,
        "verify": cmd_mcp_verify,
        "diagnose-auth": cmd_mcp_diagnose_auth,
    }
    fn = dispatch.get(cmd)
    if fn is None:
        print(_red(f"Unknown mcp subcommand: {cmd}"))
        return 1
    return fn(args)


def _dispatch_skill(args: argparse.Namespace) -> int:
    cmd = getattr(args, "skill_command", None)
    if cmd is None:
        print("Usage: noui skill {list,show,install,uninstall,docs}")
        return 1
    dispatch = {
        "list": cmd_skill_list,
        "show": cmd_skill_show,
        "install": cmd_skill_install,
        "uninstall": cmd_skill_uninstall,
        "docs": cmd_skill_docs,
    }
    fn = dispatch.get(cmd)
    if fn is None:
        print(_red(f"Unknown skill subcommand: {cmd}"))
        return 1
    return fn(args)


def _dispatch_autopilot(args: argparse.Namespace) -> int:
    cmd = getattr(args, "autopilot_command", None)
    if cmd is None:
        print(
            "Usage: noui autopilot"
            " {verify-extension,start-capture,stop-capture,resume-capture,"
            "capture-status,export,browser,list,status}"
        )
        return 1
    dispatch = {
        "verify-extension": cmd_autopilot_verify_extension,
        "capture-status": cmd_autopilot_capture_status,
        "resume-capture": cmd_autopilot_resume_capture,
        "start-capture": cmd_autopilot_start_capture,
        "stop-capture": cmd_autopilot_stop_capture,
        "export": cmd_autopilot_export,
        "list": cmd_autopilot_list,
        "status": cmd_autopilot_status,
        "browser": cmd_autopilot_browser,
    }
    fn = dispatch.get(cmd)
    if fn is None:
        print(_red(f"Unknown autopilot subcommand: {cmd}"))
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
        "skill": _dispatch_skill,
        "autopilot": _dispatch_autopilot,
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
