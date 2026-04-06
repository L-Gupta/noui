# NoUI

NoUI turns websites into authenticated APIs for agents. Record a browser session once, then call the underlying APIs directly — no UI automation required.

## Architecture

```
Browser Extension (NoUI Recorder)
    → record mode: Login Recording | Workflow Recording
    ↓
NoUI Backend (FastAPI, port 8002)
    → login-sessions/    — captures login flows
    → workflow-sessions/ — captures workflow API traffic
    → shared: clicks, url-events, HAR upload
    ↓
Compiler
    → login/  — login session → Tabby Application + ServiceProfile
    → mcp/    — workflow session → FastMCP server + manifest
    ↓
Output
    → login_recordings/    — Tabby bundle JSON files
    → mcp_servers/<app>/<server_id>/  — runnable FastMCP packages
```

## Prerequisites

- Python 3.11+
- Chrome browser
- (Optional) Tabby running at `http://localhost:8080` for live auth

## Setup

```bash
# 1. Install dependencies (from noui/ directory)
pip install -e .
# or with poetry:
poetry install

# 2. Copy and fill in environment variables
cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY, TABBY_ADMIN_TOKEN if needed

# 3. Load the Chrome extension
# Chrome → chrome://extensions → Load unpacked → select noui/extension/
```

## Developer Flow

### 0. Install the NoUI skills

Install the NoUI skills into your project so Claude Code can guide you through the full workflow:

```bash
npx skills add <github-owner>/noui
```

Then run `/setup` in Claude Code to configure the environment. The available skills are:

| Skill | Purpose |
|---|---|
| `/setup` | One-time setup: venv, deps, `.env`, Chrome extension |
| `/record-login` | Record a login flow and register it with Tabby (authenticated apps) |
| `/record-workflow` | Record a workflow and export it as a FastMCP server |
| `/mcp` | Start, stop, list, and connect generated MCP servers to Claude Code |

---

### 1. Start the backend

```bash
python cli/main.py start
# Backend runs at http://localhost:8002
# Docs at http://localhost:8002/docs
```

### 2. Record a login session

```bash
python cli/main.py login record "HubSpot" "https://app.hubspot.com/login"
# → Prints a session ID and extension instructions
```

In Chrome:
1. Click the **NoUI Workflow Recorder** extension icon
2. Navigate to the login URL and record your login
3. Click **Complete** in the extension popup when done

### 3. Export and review the login bundle

```bash
python cli/main.py login export <session_id>
# → Writes bundle to login_recordings/noui-<session_id>-bundle.json

python cli/main.py login review login_recordings/noui-<session_id>-bundle.json
# → Prints validation status and review items
```

### 4. Register with Tabby (optional — requires Tabby running)

```bash
python cli/main.py login register login_recordings/noui-<session_id>-bundle.json
# → Provisions Application + ServiceProfile in Tabby
# → Prints tabby_profile_id

python cli/main.py login validate login_recordings/noui-<session_id>-bundle.json
# → Waits for profile to become HEALTHY
```

### 5. Record a workflow session

```bash
python cli/main.py workflow record "Create Contact" "https://app.hubspot.com"
# → Prints a session ID and extension instructions
```

In Chrome, perform the workflow you want to automate, then click **Complete** in the extension.

### 6. Export the workflow as a FastMCP server

```bash
python cli/main.py workflow export-mcp <session_id> --profile hubspot-standard
# → Compiles HAR + click events into a FastMCP server
# → Writes to mcp_servers/create-contact/<server_id>/
```

### 7. Run the generated MCP server

```bash
python cli/main.py mcp start <server_id>
python cli/main.py mcp status <server_id>
python cli/main.py mcp stop <server_id>
```

## Generated MCP Output Shape

```
mcp_servers/
  <app_slug>/
    <server_id>/
      server.py            # FastMCP entrypoint
      tools.json           # Tool inventory
      manifest.json        # Server manifest (lifecycle + auth metadata)
      noui_runtime/
        auth.py            # Runtime auth adapter (fetches live creds from Tabby)
      operations/
        <tool_name>.py     # One file per generated tool
```

### Manifest schema

```json
{
  "schema_version": "1",
  "server_id": "hubspot-create-contact-abc12345",
  "app": {"name": "HubSpot", "slug": "hubspot"},
  "workflow": {"id": "create-contact", "name": "Create Contact", "workflow_session_id": "uuid"},
  "auth": {"tabby_profile_id": "hubspot-standard"},
  "runtime": {"type": "fastmcp", "entrypoint": "server.py", "transport": "stdio", "direct_execution": true},
  "tools": [{"name": "create_contact", "description": "...", "method": "POST", "path": "/contacts/v1/contact"}],
  "artifacts": {"server_file": "server.py", "tools_file": "tools.json", "files": ["..."]},
  "generation": {"generated_at": "2026-04-06T00:00:00Z", "generator": "noui", "generator_version": "v1"}
}
```

## Backend API

| Method | Path | Description |
|--------|------|-------------|
| POST | /login-sessions | Create login session |
| POST | /login-sessions/{id}/start | Start recording |
| POST | /login-sessions/{id}/complete | Mark complete |
| POST | /login-sessions/{id}/analyze | Generate Tabby bundle |
| GET  | /login-sessions/{id}/bundle | Retrieve bundle |
| POST | /workflow-sessions | Create workflow session |
| POST | /workflow-sessions/{id}/start | Start recording |
| POST | /workflow-sessions/{id}/complete | Mark complete |
| POST | /workflow-sessions/{id}/export-mcp?tabby_profile_id=xxx | Compile to FastMCP |
| POST | /clicks | Store click event |
| POST | /url-events | Store URL navigation event |
| POST | /capture-sessions/{id}/har | Upload HAR (extension compat) |
| GET  | /health | Backend health check |

Interactive docs: http://localhost:8002/docs

## Key Design Decisions

- **Login and workflow recordings are separate paths** — different DB tables, different APIs, different compilers
- **Generated MCPs resolve auth at runtime** — no credentials baked into static config; Tabby provides live session material via `tabby_profile_id`
- **One directory per generated server** — `mcp_servers/<app_slug>/<server_id>/` with a `manifest.json` for CLI discovery
- **FastMCP as the generated runtime** — direct HTTP execution, not browser automation
- **Tabby stays external** — v1 registers against an external Tabby instance; embedding is deferred

## What's Not in V1

- Embedding Tabby into this repo
- Full ABCD/WDL convergence
- Multi-tenant hardening
- General-purpose session replay beyond login bootstrap
