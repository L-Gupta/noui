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
4.  Maintain authenticated sessions via Tabby
5.  Expose everything as an MCP endpoint
6.  Agents call APIs instead of clicking UI

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
Output
    → login_recordings/              — Tabby bundle JSON files
    → mcp_servers/<app>/<server_id>/ — runnable FastMCP packages
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
- Chrome browser
- (Optional) Tabby running at `http://localhost:8080` for authenticated apps

### Install

```bash
# From the noui/ directory

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

Install the NoUI skills into your project so your Agent (Claude Code, Codex, OpenClaw...) can guide you through the full workflow:

```bash
npx skills add adoptai/noui
```

Then run `/noui-setup` in Claude Code to configure the environment.

| Skill | Purpose |
|---|---|
| `/noui-setup` | One-time setup: venv, deps, `.env`, Chrome extension, Tabby CLI reference |
| `/noui-record-login` | Record a login flow and register it with Tabby |
| `/noui-record-workflow` | Record a workflow and export it as a FastMCP server |
| `/noui-generalize` | Rename raw API parameters to natural-language equivalents post-export |
| `/noui-mcp` | Start, stop, list, and connect generated MCP servers to Claude Code |

------------------------------------------------------------------------

## 🛠️ Developer Flow

### Unauthenticated apps

```bash
# 1. Create a workflow session
.venv/bin/python cli/main.py workflow record "Fetch Results" "https://example.com"

# 2. Record in Chrome (extension → Workflow Recording mode → perform workflow → Complete)

# 3. Export as FastMCP server
.venv/bin/python cli/main.py workflow export-mcp <session_id>

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
.venv/bin/python cli/main.py workflow export-mcp <session_id> --profile <tabby_profile_id>

# 4. Start the MCP server
.venv/bin/python cli/main.py mcp start <server_id>
```

------------------------------------------------------------------------

## 📦 Generated MCP Output

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

------------------------------------------------------------------------

## 🖥️ CLI Reference

```
noui start / stop / status

noui login record <app> <url>          create login session
noui login export / review / register / validate <session_id|bundle>
noui login import <session_id> [--validate]

noui workflow record <name> <url>      create workflow session
noui workflow list / captures
noui workflow export-mcp <session_id> [--profile <tabby_profile_id>]

noui mcp list / status / start / stop <server_id>

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
| POST | `/workflow-sessions/{id}/export-mcp?tabby_profile_id=xxx` | Compile to FastMCP |
| POST | `/clicks` | Store click event |
| POST | `/url-events` | Store URL navigation event |
| POST | `/capture-sessions/{id}/har` | Upload HAR (extension compat) |
| GET  | `/health` | Backend health check |

Interactive docs: http://localhost:8002/docs

------------------------------------------------------------------------

## 🔥 Status

Early open source --- expect rough edges.
Contributions welcome.

------------------------------------------------------------------------

## 📢 Closing

Computer-use agents were step one.

**NoUI is what comes next.**
