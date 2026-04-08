---
name: noui-autopilot
description: Use this skill when the user wants to automatically record a browser workflow and generate an MCP server without manually using the Chrome extension popup. Triggers on "autopilot record", "auto-record a workflow", "automatically capture a workflow", "noui autopilot", "record this website automatically", "generate an MCP from this site", or "I want to automate this website without manual recording". This skill makes YOU (Claude Code) the browser agent — you drive the browser directly.
---

# NoUI Autopilot Recording

You ARE the browser automation agent. You will drive a real Chrome browser through the NoUI extension to record a workflow, then compile it into a FastMCP server. No separate agent or API key is needed — you control the browser directly via HTTP commands.

All commands run from the `noui/` directory using `.venv/bin/python cli/main.py`.

**Prerequisite:** `/noui-setup` must be complete. Chrome must be open with the NoUI extension loaded and connected to `localhost:8002`.

---

## Critical Rules (Never Violate)

- **YOU are the browser agent.** Do not call any external AI API. You read page state, decide what to do, and execute browser commands yourself.
- **ALWAYS** start the backend before doing anything else.
- **NEVER** type raw passwords into chat output or logs. When you type credentials into the browser, use the browser command — the value goes to the extension, not to the conversation.
- **ALWAYS** call `get_page_summary` before clicking or typing to understand what elements are available.
- **NEVER** click Send, Submit, Pay, Delete, Publish, or Invite buttons unless the user explicitly allowed that side effect.
- **ALWAYS** stop capture before exporting MCP.
- **NEVER** skip capture validation — if no API calls were recorded, do not export an empty MCP.
- If you encounter MFA, captcha, or any challenge you cannot handle, **STOP and ask the user** to complete it manually in the browser, then resume.

---

## Step 0 — Gather Input

Ask the user for:
1. **Website URL** (required)
2. **Username and password** (if the site requires login)
3. **What they want to do** — the task description (required)
4. **Any side effects to allow or forbid** (optional)

Example:
> User: "Autopilot record on https://app.example.com — log in as admin@example.com / mypass123, then create a draft invoice for Acme Corp for $500"

---

## Step 1 — Start the Backend

```bash
.venv/bin/python cli/main.py start
```

Verify:

```bash
.venv/bin/python cli/main.py status
```

---

## Step 2 — Create Workflow Session and Capture Session

```bash
.venv/bin/python cli/main.py workflow record "<TaskName>" "<website_url>"
```

Note the `session_id`, `project_id`, and `process_id` from the output.

Then create a capture session:

```bash
curl -s -X POST http://localhost:8002/processes/<process_id>/capture-sessions \
  -H 'Content-Type: application/json' \
  -d '{"click_tracking": true, "url_monitoring": true, "har_capture": true}' | python3 -m json.tool
```

Note the capture session `id`.

Start the capture session:

```bash
curl -s -X PUT http://localhost:8002/capture-sessions/<capture_session_id>/start \
  -H 'Content-Type: application/json' | python3 -m json.tool
```

---

## Step 3 — Navigate to the Website

```bash
.venv/bin/python cli/main.py autopilot browser navigate url=<website_url>
```

Wait 2 seconds for the page to load, then read the page:

```bash
.venv/bin/python cli/main.py autopilot browser get_page_summary
```

---

## Step 4 — Login (if credentials provided)

Read the page to find the login form:

```bash
.venv/bin/python cli/main.py autopilot browser get_page_summary
```

Then type credentials and submit. Example flow:

```bash
# Type username
.venv/bin/python cli/main.py autopilot browser type_into_label label=Username text=admin@example.com

# Type password
.venv/bin/python cli/main.py autopilot browser type_into_label label=Password text=secret123

# Click sign in
.venv/bin/python cli/main.py autopilot browser click_by_text "Sign In"
```

Wait for the page to settle, then verify login succeeded:

```bash
sleep 2
.venv/bin/python cli/main.py autopilot browser get_page_summary
```

If you see MFA/captcha, tell the user:
> "I see an MFA/captcha challenge. Please complete it in the browser, then tell me when you're done."

---

## Step 5 — Perform the Workflow Task

This is the core loop. You read the page, decide what to do, and execute:

1. **Read page state:**
   ```bash
   .venv/bin/python cli/main.py autopilot browser get_page_summary
   ```

2. **Decide** what action to take based on the task description and visible elements.

3. **Execute** the action:
   ```bash
   # Click a button or link
   .venv/bin/python cli/main.py autopilot browser click_by_text "New Invoice"

   # Click by CSS selector
   .venv/bin/python cli/main.py autopilot browser click_element selector=#create-btn

   # Type into a field
   .venv/bin/python cli/main.py autopilot browser type_into_label label="Customer" text="Acme Corp"

   # Select a dropdown option
   .venv/bin/python cli/main.py autopilot browser select_option selector=#status value=draft

   # Press Enter
   .venv/bin/python cli/main.py autopilot browser press_key Enter

   # Wait for an element to appear
   .venv/bin/python cli/main.py autopilot browser wait_for_selector selector=.success-message

   # Navigate to a URL
   .venv/bin/python cli/main.py autopilot browser navigate url=https://app.example.com/invoices/new
   ```

4. **Repeat** until the task is complete.

### Safety Checks

Before clicking any button that could have side effects, check if it's in the forbidden list:
- **STOP before** Send, Submit, Pay, Delete, Publish, Invite — unless explicitly allowed
- **ASK the user** if uncertain about a destructive action

### Available Browser Commands

| Command | Primary Param | Description |
|---------|--------------|-------------|
| `get_page_info` | — | Get current URL and title |
| `get_page_summary` | — | List all visible interactive elements |
| `navigate` | `url` | Navigate to a URL |
| `click_element` | `selector` | Click by CSS selector |
| `click_by_text` | `text` | Click first visible element containing text |
| `type_text` | `selector`, `text` | Type into a field by selector |
| `type_into_label` | `label`, `text` | Type into field matching label/placeholder |
| `select_option` | `selector`, `value` | Select dropdown option |
| `press_key` | `key` | Press a keyboard key (Enter, Tab, Escape) |
| `wait_for_selector` | `selector` | Wait up to 10s for element |
| `wait_for_url` | `url_substring` | Wait up to 10s for URL change |
| `take_screenshot` | — | Capture screenshot (use sparingly) |
| `query_elements` | `selector` | Query elements by CSS selector |
| `eval_js` | `code` | Evaluate JavaScript in the page |

---

## Step 6 — Stop Capture

Stop the capture session (this triggers HAR upload by the extension):

```bash
curl -s -X PUT http://localhost:8002/capture-sessions/<capture_session_id>/stop \
  -H 'Content-Type: application/json' | python3 -m json.tool
```

Complete the workflow session:

```bash
curl -s -X POST http://localhost:8002/workflow-sessions/<workflow_session_id>/complete \
  -H 'Content-Type: application/json' | python3 -m json.tool
```

Wait 3 seconds for the HAR upload to finish:

```bash
sleep 3
```

---

## Step 7 — Verify Capture

Check that a HAR file was uploaded:

```bash
curl -s http://localhost:8002/capture-sessions/<capture_session_id> | python3 -m json.tool
```

Also list captures to see the session:

```bash
.venv/bin/python cli/main.py workflow captures
```

---

## Step 8 — Export MCP

```bash
.venv/bin/python cli/main.py workflow export-mcp <workflow_session_id> \
  --capture-session <capture_session_id>
```

If the user has a Tabby profile, add `--profile-slug <slug>`.

Note the `server_id` from the output.

---

## Step 9 — Install and Report

```bash
.venv/bin/python cli/main.py mcp install <server_id> claude-code
```

Report to the user:
- Server ID
- Number of tools generated
- Output path
- Install command (already run)

---

## Decision Flow

```
Start
  |
  +-- Backend running? --> No --> Step 1: start
  |                    --> Yes --> continue
  |
  Step 0: Gather website URL, credentials, task description
  |
  Step 2: Create workflow session + capture session, start capture
  |
  Step 3: Navigate to website
  |
  +-- Login needed? --> Yes --> Step 4: Login flow
  |                 --> No  --> continue
  |
  Step 5: Perform task (read page, decide, act, repeat)
  |
  +-- MFA/captcha? --> Ask user to complete, wait, resume
  +-- Dangerous button? --> Check allowed list, ask if unsure
  |
  Step 6: Stop capture
  |
  Step 7: Verify HAR captured
  |   +-- No HAR? --> Tell user, suggest re-recording manually
  |
  Step 8: Export MCP
  |   +-- 0 tools? --> Warn user, capture may not have API calls
  |
  Step 9: Install + report
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Browser command returns 504 timeout | Extension not connected — ask user to open Chrome with the NoUI extension |
| `get_page_summary` shows no elements | Page may still be loading — wait 2-3s and retry |
| HAR file not found after stop | Extension may not have uploaded — wait longer, check `workflow captures` |
| 0 tools after export | The workflow only recorded HTML pages, not API calls — the site may use server-side rendering |
| Login redirect loop | The session cookie may not persist — check if the extension is on the same tab |
| `click_by_text` says "not found" | Try `get_page_summary` to see exact element text, use `click_element` with selector instead |

---

## CLI Command Reference

| Command | Description |
|---------|-------------|
| `.venv/bin/python cli/main.py start` | Start backend |
| `.venv/bin/python cli/main.py status` | Check backend health |
| `.venv/bin/python cli/main.py workflow record <name> <url>` | Create workflow session |
| `.venv/bin/python cli/main.py workflow captures` | List capture sessions |
| `.venv/bin/python cli/main.py workflow export-mcp <id> --capture-session <cap_id>` | Export to MCP |
| `.venv/bin/python cli/main.py autopilot browser <cmd> [args]` | Execute browser command |
| `.venv/bin/python cli/main.py autopilot list` | List autopilot runs |
| `.venv/bin/python cli/main.py autopilot status <run_id>` | Show run details |
| `.venv/bin/python cli/main.py mcp install <server_id> claude-code` | Install MCP server |
