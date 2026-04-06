# NoUI Skills

Claude Code skills for the full NoUI workflow — from environment setup to recording, compiling, and running FastMCP servers.

Compatible with any agent that supports the [Skills](https://github.com/vercel-labs/skills) framework — Claude Code, Cursor, Cline, and others.

## Installation

```bash
npx skills add adoptai/noui
```

Or install a specific skill only:

```bash
npx skills add adoptai/noui -s setup
```

Skills install to `.agents/skills/` in your current project and become available as slash commands immediately.

## Skills

Skills cover the end-to-end NoUI cycle. Each phase hands off to the next.

---

### Phase 1 — Setup

#### `/noui-setup`

One-time environment setup: create the venv, install dependencies via `poetry install --no-root`, configure `.env`, and load the Chrome extension.

```
python3 -m venv .venv
→ poetry install --no-root
→ cp .env.example .env (set ANTHROPIC_API_KEY)
→ Load noui/extension/ in Chrome (Developer mode)
→ Verify with: .venv/bin/python cli/main.py status
```

---

### Phase 2 — Login Recording (authenticated apps only)

#### `/noui-record-login`

Record a login flow for an app that requires authentication and register it with Tabby to produce a `tabby_profile_id`.

```
start backend
→ login record "<App>" "<url>"
→ Chrome: inject login recorder via service worker console → perform login → stop recorder + POST /complete
→ login export → login review (check generator_valid)
→ login register (note tabby_profile_id)
→ login validate (wait for HEALTHY)
→ tabby session ensure (start live browser session worker)
```

Output: `tabby_profile_id` + live session worker → used in `/record-workflow --profile`

---

### Phase 3 — Workflow Recording

#### `/noui-record-workflow`

Record a browser workflow and compile it into a runnable FastMCP server. Supports both authenticated and unauthenticated paths.

```
Path A (authenticated):
  start backend → workflow record → Chrome: Start Capture → perform workflow → Stop
  → workflow captures (note capture_session_id)
  → workflow export-mcp <session_id> --capture-session <capture_session_id> --profile <tabby_profile_id>

Path B (unauthenticated / public API):
  start backend → workflow record → Chrome: Start Capture → perform workflow → Stop
  → workflow captures (note capture_session_id)
  → workflow export-mcp <session_id> --capture-session <capture_session_id>
```

Output: `server_id` → used in `/noui-mcp`

---

### Phase 4 — MCP Server Management

#### `/noui-mcp`

Start, stop, list, and connect generated FastMCP servers to Claude Code.

```
mcp list                    → see all generated servers
mcp start <server_id>       → spawn server process
mcp status <server_id>      → check running state
mcp stop <server_id>        → stop server
→ Add to ~/.claude.json      → restart Claude Code → tools available
```

## CLI Reference

All commands: `.venv/bin/python cli/main.py <command>` from the `noui/` directory.

| Command | Purpose |
|---|---|
| `start` / `stop` / `status` | Backend lifecycle |
| `login record / list / export / review / register / validate / import` | Login session lifecycle |
| `workflow record / list / export-mcp` | Workflow session lifecycle |
| `mcp list / start / stop / status` | MCP server lifecycle |
