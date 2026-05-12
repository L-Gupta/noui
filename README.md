# NoUI

> **Skip the UI. Turn any website into fast, reliable APIs for
> agents.**\
> *Go beyond Claw. Call the underlying APIs.*\
> *Skip Computer-use Agents.*

------------------------------------------------------------------------

## 🚀 What is NoUI?

**NoUI turns any website into an API your agents can call.**

Instead of automating clicks and scraping the UI, NoUI:
1. Records how you use a website
2. Extracts the underlying APIs
3. Converts them into callable Python functions
4. Exposes them via MCP for agents

No clicks. No DOM parsing. No brittle automation.

------------------------------------------------------------------------

## ⚡ Why NoUI?

Computer-use agents simulate humans:
- 🐢 Slow (UI loops, page loads)
- 💸 Expensive (token-heavy, step-heavy)
- 🧱 Fragile (break on UI changes)

**NoUI executes software directly:**
- ⚡ **Fast** --- direct API calls
- 💸 **Cheap** --- fewer steps, fewer tokens
- 🎯 **Reliable** --- uses the same APIs the app uses internally

> **Stop automating clicks. Execute software.**

------------------------------------------------------------------------

## 🧠 How it works

1.  Record a session (Chrome extension + voice)
2.  Extract HAR traces + intent
3.  Convert into Python API functions
4.  Execute tools from inside the authenticated Tabby browser session
5.  Expose everything as an MCP endpoint
6.  Agents call APIs instead of clicking UI

### Execution

Generated tools run from **inside** the authenticated Tabby browser, not from a
Python HTTP client. Each operation opens a WebSocket to Tabby's CDP endpoint
(`localhost:9222`), locates the tab for the target domain, and calls
`fetch(url, {credentials: 'include'})` via `Runtime.evaluate`. The real browser's
TLS fingerprint and cookies are used — no credential extraction, and no
Akamai/Cloudflare false positives.

The legacy Python-side path (`httpx` + `resolve_auth()`) is still available for
server-to-server APIs that aren't reachable from the browser origin; opt in
explicitly with `noui workflow export --execution-mode http`.

------------------------------------------------------------------------

## 🏗️ Architecture

```
Browser Extension (NoUI Recorder)
    → Login Recording | Workflow Recording modes
    ↓ (HAR + click events)
NoUI Backend (FastAPI, port 8002)
    → login-sessions/    — captures login flows
    → workflow-sessions/ — captures workflow API traffic
    → shared: clicks, url-events, HAR upload
    ↓
Compiler
    → login/  — login session → Tabby Application + ServiceProfile bundle
    → mcp/    — workflow session → FastMCP server + manifest
    ↓
Output (all under workbench/)
    → workbench/login_recordings/              — Tabby bundle JSON files
    → workbench/mcp_servers/<app>/<server_id>/ — runnable FastMCP packages
    → workbench/skills/<app>/<skill_id>/       — installable Claude Code skills
    ↓
Tabby Runtime  (persistent browser sessions + live auth)
    ↓
MCP → Claude / ChatGPT / Agents
```

------------------------------------------------------------------------

## 🔑 Core Components

### 🧩 HAR → API Compiler

-   Parses browser network traffic (HAR)
-   Groups requests into logical workflows
-   Generates clean Python functions

### 🐾 Tabby Runtime

-   Keeps browser sessions alive in the cloud
-   Handles cookies, headers, auth
-   Streams VNC for login / 2FA when needed

### 🔌 MCP Server

-   Exposes generated APIs as tools
-   Works with Claude, ChatGPT, and MCP-compatible agents

------------------------------------------------------------------------

## 🎯 What you can do

-   Automate websites with no public APIs
-   Turn internal tools into agent-ready SDKs
-   Replace brittle browser automation workflows
-   Build production-grade agents that actually scale

------------------------------------------------------------------------

## ⚡ Example

``` python
def create_invoice(customer_id, amount):
    return call_api(
        method="POST",
        endpoint="/api/invoices",
        headers=session_headers,
        json={
            "customer_id": customer_id,
            "amount": amount
        }
    )
```

Then your agent simply says:

> "Create an invoice for customer X"

NoUI executes it directly.

------------------------------------------------------------------------

## 🔐 Authentication Flow

1.  NoUI spins up a remote browser session
2.  Streams it via VNC
3.  You complete login / 2FA once
4.  Tabby maintains the session

Agents reuse the authenticated context automatically.

------------------------------------------------------------------------

## ⚔️ NoUI vs Computer-Use Agents

|               | Computer-Use Agents  | NoUI               |
|---------------|----------------------|--------------------|
| Speed         | Slow (UI loops)      | Fast (direct APIs) |
| Cost          | High                 | Low                |
| Reliability   | Breaks on UI changes | More Stable        |
| Approach      | Simulates humans     | Executes software  |

------------------------------------------------------------------------

## 🧠 Philosophy

> Websites already expose APIs.
> The UI is just a layer on top.

NoUI removes that layer.

------------------------------------------------------------------------

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Docker (for Tabby)
- Chrome browser

### Clone with submodules

> ⚠️ **NoUI includes Tabby as a git submodule.** You must clone recursively,
> or the `tabby/` directory will be empty and `noui tabby start` will fail.

```bash
git clone --recursive https://github.com/adoptai/noui.git

# Or, if you already cloned without --recursive:
git submodule update --init
```

### Install

```bash
cd noui

# 1. Create a venv and install dependencies
python3 -m venv .venv
.venv/bin/python -m pip install poetry
.venv/bin/python -m poetry install --no-root

# 2. Configure environment
cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY (required)
#            TABBY_API_HOST, TABBY_ADMIN_TOKEN (authenticated apps only)

# 3. Load the Chrome extension
# Chrome → chrome://extensions → Developer mode → Load unpacked → select noui/extension/

# 4. Start the backend
.venv/bin/python cli/main.py start
# Backend runs at http://localhost:8002
# Interactive API docs at http://localhost:8002/docs
```

------------------------------------------------------------------------

## 🤖 Agent Skills

Start by installing the NoUI entry-point skill — a small discovery guide that lists the core workflow skills and optional demos, and tells your agent (Claude Code, Codex, OpenClaw…) how to install each one:

```bash
npx skills add https://github.com/adoptai/noui --skill noui
```

Then invoke `/noui` in your agent — it will list the core workflow skills and optional demos, and you pick what to install. After that, `/noui-setup` configures the project environment.

> **Human users** who prefer an interactive picker can run `npx skills add https://github.com/adoptai/noui` (no `--skill` flag) and tick the skills they want. AI agents must use the per-skill `--skill <name>` form — the interactive selector blocks on stdin.

### Available skills

| Skill | Purpose |
|---|---|
| `/noui` | Entry point — lists core and demo install commands |
| `/noui-setup` | One-time setup: venv, deps, `.env`, Chrome extension, Tabby CLI reference |
| `/noui-record-login` | Record a login flow and register it with Tabby |
| `/noui-record-workflow` | Record a workflow and export it as a FastMCP server or Claude Code Skill |
| `/noui-generalize` | Rename raw API parameters to natural-language equivalents post-export |
| `/noui-autopilot` | Auto-record workflows without the manual extension popup |
| `/noui-generate-mcp` | Start, stop, list, and connect generated MCP servers to Claude Code |
| `/noui-generate-skill` | List, install, and uninstall generated Claude Code Skills across agents |
| `/airbnb-search-places` | Demo: anonymous Airbnb place search |
| `/expedia-stay-search` | Demo: authenticated Expedia stay search via Tabby |

------------------------------------------------------------------------

## 🛠️ Developer Flow

### Unauthenticated apps

```bash
# 1. Create a workflow session
.venv/bin/python cli/main.py workflow record "Fetch Results" "https://example.com"

# 2. Record in Chrome (extension → Workflow Recording mode → perform workflow → Complete)

# 3. Export as FastMCP server
.venv/bin/python cli/main.py workflow export --as mcp <session_id>

# 4. Start the MCP server
.venv/bin/python cli/main.py mcp start <server_id>
```

### Authenticated apps (with Tabby)

```bash
# 0. Provision Tabby (first time only)
.venv/bin/python cli/main.py tabby start
.venv/bin/python cli/main.py tabby setup   # interactive

# 1. Record login and register with Tabby
.venv/bin/python cli/main.py login record "HubSpot" "https://app.hubspot.com/login"
# (record in Chrome using Login Recording mode)
.venv/bin/python cli/main.py login import <session_id> --validate
# → prints tabby_profile_id

# 2. Ensure a live browser session
.venv/bin/python cli/main.py tabby session ensure

# 3. Record and export the workflow
.venv/bin/python cli/main.py workflow record "Create Contact" "https://app.hubspot.com"
# (record in Chrome using Workflow Recording mode)
.venv/bin/python cli/main.py workflow export --as mcp <session_id> --profile <tabby_profile_id>

# 4. Start the MCP server
.venv/bin/python cli/main.py mcp start <server_id>
```

------------------------------------------------------------------------

## 📦 Generated MCP Output

```
workbench/mcp_servers/
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

------------------------------------------------------------------------

## 🖥️ CLI Reference

```
noui start / stop / status

noui login record <app> <url>          create login session
noui login export / review / register / validate <session_id|bundle>
noui login import <session_id> [--validate]

noui workflow record <name> <url>      create workflow session
noui workflow list / captures
noui workflow export <session_id> --as {mcp|skill|both} [--profile <tabby_profile_id>]

noui mcp list / status / start / stop <server_id>
noui skill list / show
noui skill install   <skill_id> <agent> [--project]    # agent: claude-code|codex|cline|opencode|agents
noui skill uninstall <skill_id> <agent> [--project]

noui tabby status / start / stop [--infra]
noui tabby setup [--profiles <id...>] [--force]
noui tabby session status / ensure [--profile] / stop
```

------------------------------------------------------------------------

## 🔌 Backend API

| Method | Path | Description |
|--------|------|-------------|
| POST | `/login-sessions` | Create login session |
| POST | `/login-sessions/{id}/start` | Start recording |
| POST | `/login-sessions/{id}/complete` | Mark complete |
| POST | `/login-sessions/{id}/analyze` | Generate Tabby bundle |
| GET  | `/login-sessions/{id}/bundle` | Retrieve bundle |
| POST | `/workflow-sessions` | Create workflow session |
| POST | `/workflow-sessions/{id}/start` | Start recording |
| POST | `/workflow-sessions/{id}/complete` | Mark complete |
| POST | `/workflow-sessions/{id}/export?as=mcp\|skill\|both` | Compile to FastMCP, Skill, or both |
| POST | `/clicks` | Store click event |
| POST | `/url-events` | Store URL navigation event |
| POST | `/capture-sessions/{id}/har` | Upload HAR (extension compat) |
| GET  | `/health` | Backend health check |

Interactive docs: http://localhost:8002/docs

------------------------------------------------------------------------

## 🔗 Tabby compatibility

NoUI depends on [Tabby](https://github.com/adoptai/tabby) for authenticated
browser sessions. To keep behavior deterministic across releases, NoUI pins a
specific SHA on Tabby's `tabby-noui` branch via a git submodule.

- **Current pin:** `tabby-noui @ d212467`
- **Branch:** `tabby-noui`
- **Submodule path:** `tabby/` (inside this repository)

Using a different Tabby revision is unsupported. If you need to run against a
local Tabby checkout for development, set the `TABBY_DIR` environment variable
to override the submodule path. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
submodule-bump workflow.

------------------------------------------------------------------------

## 🧩 Chrome extension

v1 ships as a **load-unpacked** extension — there is no Chrome Web Store listing yet.

```
Chrome → chrome://extensions → Developer mode → Load unpacked → select noui/extension/
```

The backend URL defaults to `http://localhost:8002`. To point the extension at
a different backend, open the extension popup → Settings.

------------------------------------------------------------------------

## 📚 Project docs

- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, PR process, bumping the Tabby submodule
- [SECURITY.md](SECURITY.md) — responsible disclosure
- [CHANGELOG.md](CHANGELOG.md) — release history
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)

------------------------------------------------------------------------

## 🔥 Status

Early open source --- expect rough edges.
Contributions welcome.

------------------------------------------------------------------------

## 📢 Closing

Computer-use agents were step one.

**NoUI is what comes next.**
