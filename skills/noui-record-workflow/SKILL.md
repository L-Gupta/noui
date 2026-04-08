---
name: noui-record-workflow
description: Use this skill when the user wants to record a browser workflow and export it as a FastMCP server. Triggers on "record a workflow", "export as MCP", "generate a FastMCP server", "noui workflow record", "workflow export-mcp", "capture a workflow", "turn a workflow into an MCP tool", "create an MCP server from a website", or "I want to automate this workflow". Covers both authenticated (with tabby_profile_id) and unauthenticated (public API) sub-paths.
---

# NoUI Record Workflow

Record a browser workflow and compile it into a runnable FastMCP server. Works for both authenticated apps (requires a `tabby_profile_id` from `/noui-record-login`) and public apps with no auth.

All commands run from the `noui/` directory using `.venv/bin/python cli/main.py`.

**Prerequisite:** `/noui-setup` must be complete. For authenticated apps (Path A), `/noui-record-login` must also be complete, you must have the `tabby_profile_id`, **and a live Tabby browser session must be running** (`tabby session ensure` from Step 8 of `/noui-record-login`).

---

## Mode Selection

**Ask the user (or infer from context) before proceeding:**

| App requires login? | Path |
|---|---|
| Yes — have `tabby_profile_id` from `/noui-record-login` | **Path A** — authenticated |
| No — public API, no credentials needed | **Path B** — unauthenticated |

The steps are identical except that Path A passes `--profile <tabby_profile_id>` to `workflow export-mcp`.

---

## Critical Rules (Never Violate)

- **ALWAYS** start the backend before asking the user to record
- **NEVER** skip `workflow export-mcp` — recording alone produces no server
- For Path A: **NEVER** omit `--profile` — without it the generated server makes unauthenticated requests and will fail against protected endpoints
- **ALWAYS** note the `server_id` printed after export — it is required for all `mcp` commands
- **ALWAYS** create the workflow session with the CLI before the user records in Chrome — the session_id is needed for export

---

## Step 1 — Start the Backend

```bash
.venv/bin/python cli/main.py start
```

Spawns a detached FastAPI server at `http://localhost:8002`. Verify with:

```bash
.venv/bin/python cli/main.py status
```

**If startup fails:** See the troubleshooting table in `/noui-setup`.

---

## Step 2 — Create a Workflow Recording Session

```bash
.venv/bin/python cli/main.py workflow record "<WorkflowName>" "<start-url>"
```

Examples:

```bash
# Path A — authenticated
.venv/bin/python cli/main.py workflow record "Create Contact" "https://app.hubspot.com/contacts"

# Path B — unauthenticated
.venv/bin/python cli/main.py workflow record "Fetch Posts" "https://jsonplaceholder.typicode.com"
```

The CLI creates a session and prints the `session_id`. Note it.

---

## Step 3 — Record in Chrome

Tell the user to perform these steps in the **NoUI Workflow Recorder** extension:

1. Click the extension icon in the Chrome toolbar
2. Create a **Project** (or select an existing one)
3. Click **Start Capture** to begin recording
4. Perform the complete workflow you want to automate
5. Click **Stop** when done

After stopping, get the **capture session ID** from the CLI:

```bash
.venv/bin/python cli/main.py workflow captures
```

Lists all capture sessions with their IDs and statuses. Note the `id` of the most recent `stopped` session — that is your `capture_session_id`.

> For Path A (authenticated apps): navigate to the authenticated starting point manually before starting capture — credentials are managed separately via Tabby.

---

## Step 4 — Export as FastMCP Server

Pass both the `session_id` (from Step 2) and the `capture_session_id` (from Step 3):

### Path A — Authenticated

```bash
.venv/bin/python cli/main.py workflow export-mcp <session_id> --capture-session <capture_session_id> --profile <tabby_profile_id>
```

### Path B — Unauthenticated

```bash
.venv/bin/python cli/main.py workflow export-mcp <session_id> --capture-session <capture_session_id>
```

The CLI compiles the captured HAR and click events into a FastMCP server and writes it to:

```
mcp_servers/<app_slug>/<server_id>/
├── server.py            # FastMCP entrypoint
├── tools.json           # Tool inventory (source of truth for tool shapes)
├── manifest.json        # Server manifest (auth metadata, runtime config)
├── API.md               # Human-readable API reference (auto-generated)
├── noui_runtime/
│   └── auth.py          # Runtime auth adapter (fetches live creds from Tabby)
└── operations/
    └── <tool_name>.py   # One file per generated tool
```

On success:

```
MCP server generated:
  Server ID  : <server_id>
  Tools      : <n>
  Output     : mcp_servers/<app_slug>/<server_id>/
```

**Note the `server_id`** — pass it to `/noui-mcp` to start and connect the server.

> **Optional Step 5 — Generalize:** If the generated tools have unreadable raw API parameter names (`f_sid`, `bl`, `reqid`, `soc_app`), run `/noui-generalize`. Claude will read the tools, ask you questions about what you recorded, and rewrite the operations with natural-language parameters (`origin`, `destination`, `departure_date`) so they're usable by Claude Code.

---

## Decision Flow

```
Start
  │
  ├─ App requires auth?
  │     ├─ Yes → Path A (need tabby_profile_id from /record-login)
  │     └─ No  → Path B
  │
  ├─ Backend running?
  │     ├─ No  → Step 1: start
  │     └─ Yes → continue
  │
  Step 2: workflow record "<Name>" "<url>" → note session_id
  │
  Step 3: Chrome (Start Capture → perform workflow → Stop)
    └─ get capture_session_id: workflow captures
  │
  Step 4:
    Path A: workflow export-mcp <session_id> --capture-session <cap_id> --profile <tabby_profile_id>
    Path B: workflow export-mcp <session_id> --capture-session <cap_id>
    └─ note server_id
  │
  Done → pass server_id to /mcp
```

---

## CLI Command Reference

| Command | Purpose |
|---|---|
| `.venv/bin/python cli/main.py start` | Start the NoUI backend |
| `.venv/bin/python cli/main.py status` | Check backend reachability and session counts |
| `.venv/bin/python cli/main.py workflow record "<Name>" "<url>"` | Create a workflow recording session |
| `.venv/bin/python cli/main.py workflow list` | List existing workflow sessions |
| `.venv/bin/python cli/main.py workflow captures` | List capture sessions recorded via the extension (use this to get `capture_session_id`) |
| `.venv/bin/python cli/main.py workflow export-mcp <session_id> --capture-session <cap_id>` | Compile session → FastMCP server (unauthenticated) |
| `.venv/bin/python cli/main.py workflow export-mcp <session_id> --capture-session <cap_id> --profile <id>` | Compile session → FastMCP server (authenticated) |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Backend not running | `.venv/bin/python cli/main.py start` |
| `session_id` not found | Run `workflow list` to confirm the session was created |
| Export fails: "No HAR file found for this session" | Extension did not capture HAR — confirm Capture was active during the workflow |
| Export fails: "Workflow session not found" | The `session_id` doesn't match any workflow session — run `workflow list` |
| Generated server has 0 tools | Workflow had no capturable HTTP calls — re-record navigating through the full flow |
| Path A: server fails with auth errors at runtime | Confirm `tabby_profile_id` is correct and profile is HEALTHY via `login validate` |
| `mcp_servers/` empty after export | Check `.noui-backend.log` in the repo root for compiler errors |
| Tools have unreadable raw API param names (`f_sid`, `bl`, `reqid`) | Run `/noui-generalize` |
| `API.md` missing from the generated folder | Run `.venv/bin/python cli/main.py mcp docs <server_id>` to generate it |
| `API.md` is stale after editing `tools.json` | Run `.venv/bin/python cli/main.py mcp docs <server_id>` to refresh |
