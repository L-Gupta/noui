---
name: noui-record-login
description: Use this skill when the user wants to record a login flow for an authenticated app and register it with Tabby to get a tabby_profile_id. Triggers on "record a login", "capture a login flow", "register with Tabby", "set up authentication for NoUI", "create a tabby_profile_id", "noui login record", "login recording mode", or "I need auth for my workflow".
---

# NoUI Record Login

Record a login flow for an authenticated app and register it with Tabby to produce a `tabby_profile_id`. That ID is required when exporting a workflow as a FastMCP server for an app that needs authentication.

All commands run from the `noui/` directory using `.venv/bin/python3.12 cli/main.py`.

**Prerequisite:** `/noui-setup` must be complete — venv installed, `.env` configured with `ANTHROPIC_API_KEY`, `TABBY_API_HOST`, and `TABBY_ADMIN_TOKEN`, Chrome extension loaded.

---

## Critical Rules (Never Violate)

- **ALWAYS** start the backend before asking the user to record
- **ALWAYS** use **Login Recording Mode** in the extension — not the standard Workflow Recording button
- **ALWAYS** run `login review` after export and before register — never skip it even for clean-looking recordings
- **NEVER** run `login register` if the review output shows `Generator valid: No` or any generator errors
- **NEVER** use the `tabby_profile_id` in a workflow export until `login validate` succeeds with a HEALTHY state
- **ALWAYS** note the `tabby_profile_id` printed by `login register` — it is needed for `/noui-record-workflow`

---

## Step 1 — Start the Backend

```bash
.venv/bin/python3.12 cli/main.py start
```

Spawns a detached FastAPI server at `http://localhost:8002`. Verify with:

```bash
.venv/bin/python3.12 cli/main.py status
```

**If startup fails:**

| Symptom | Fix |
|---|---|
| Port already in use | `lsof -i :8002`; kill conflicting process or set `NOUI_PORT` in `.env` |
| `uvicorn not found` | Deps not installed — run `poetry install --no-root` |
| Did not become ready | Check `.noui-backend.log` in the repo root |

---

## Step 2 — Create a Login Recording Session

```bash
.venv/bin/python3.12 cli/main.py login record "<AppName>" "<login-url>"
```

Example:

```bash
.venv/bin/python3.12 cli/main.py login record "HubSpot" "https://app.hubspot.com/login"
```

The CLI creates a session and prints the `session_id`. Note it — you need it for the export step.

---

## Step 3 — Record in Chrome Using Login Recording Mode

Tell the user to perform these steps:

1. Click the **NoUI Workflow Recorder** extension icon in the Chrome toolbar
2. Select **Login Recording Mode** — not the Workflow Recording button
3. Navigate to the login URL printed in Step 2
4. Perform the complete login flow: enter credentials, submit, and wait for the authenticated page to fully settle
5. Click **Complete** in the extension popup when the authenticated state is stable

> If the user clicks the standard Workflow Recording button instead of Login Recording Mode, the session will not capture the correct login signals. Stop, note the session as invalid, and start a new `login record` session from Step 2.

---

## Step 4 — Export the Bundle

```bash
.venv/bin/python3.12 cli/main.py login export <session_id>
```

Analyzes the captured session and writes:

```
login_recordings/<session_id8>-bundle.json
```

The bundle contains the application draft, service profile draft, inferred login steps, and review items.

---

## Step 5 — Review the Bundle

```bash
.venv/bin/python3.12 cli/main.py login review login_recordings/noui-<session_id8>-bundle.json
```

Check the output for:

- `Generator valid: No` — hard block; do not proceed to register; re-record
- `Errors:` section — must resolve before registering
- `Warnings:` section — review selector confidence; re-record if confidence is low
- `Generated login steps:` — verify the inferred steps match the actual login flow

**Only proceed to Step 6 if `Generator valid: Yes` and no errors.**

---

## Step 6 — Register with Tabby

Tabby must be reachable at `TABBY_API_HOST` (default `http://localhost:8080`) and `TABBY_ADMIN_TOKEN` must be set in `.env`.

> **If Tabby is not yet running:** run `noui tabby start` then `noui tabby setup` (interactive) to start the service and provision agent credentials before registering. See `/noui-setup` for the full Tabby CLI reference.

```bash
.venv/bin/python3.12 cli/main.py login register login_recordings/noui-<session_id8>-bundle.json
```

Provisions a Tabby Application and a STAGING ServiceProfile. On success:

```
Registered profile '<profile_id>'
  Tabby profile ID : <tabby_profile_id>
  Version state    : STAGING
```

**Record the `tabby_profile_id` value** — this is the `--profile` argument for `workflow export-mcp`.

---

## Step 7 — Validate the Profile

```bash
.venv/bin/python3.12 cli/main.py login validate login_recordings/noui-<session_id8>-bundle.json
```

Polls Tabby for up to 60 seconds waiting for the profile to reach HEALTHY state.

**If validation fails:**

| Symptom | Fix |
|---|---|
| Profile enters FAILED state | Re-record with slower, more deliberate interactions |
| Timeout (60s) | Check Tabby logs; confirm `TABBY_API_HOST` is reachable |
| Keepalive URL errors | The keepalive URL must return HTTP 200 when authenticated — redirect-only URLs are not valid |

---

## Convenience Path (clean recordings only)

```bash
.venv/bin/python3.12 cli/main.py login import <session_id>
.venv/bin/python3.12 cli/main.py login import <session_id> --validate
```

Runs export + review + register in one command (and optionally validate). Use only for recordings with no expected issues — the decomposed flow above is easier to debug.

---

## Output

On completion you have:
- `login_recordings/noui-<session_id8>-bundle.json`
- `tabby_profile_id` — printed by `login register`

Pass the `tabby_profile_id` to `/noui-record-workflow` via `--profile`.

---

## Decision Flow

```
Start
  │
  ├─ backend running?
  │     ├─ No  → Step 1: start
  │     └─ Yes → continue
  │
  Step 2: login record "<App>" "<url>" → note session_id
  │
  Step 3: Chrome (Login Recording Mode only)
    └─ wrong mode? → discard session, return to Step 2
  │
  Step 4: login export <session_id>
    └─ writes noui-<id8>-bundle.json
  │
  Step 5: login review <bundle.json>
    ├─ generator errors? → re-record (return to Step 2)
    └─ clean → continue
  │
  Step 6: login register <bundle.json> → note tabby_profile_id
  │
  Step 7: login validate <bundle.json>
    ├─ HEALTHY → done — pass tabby_profile_id to /record-workflow
    └─ FAILED  → re-record (return to Step 2)
```

---

## CLI Command Reference

| Command | Purpose |
|---|---|
| `.venv/bin/python3.12 cli/main.py start` | Start the NoUI backend |
| `.venv/bin/python3.12 cli/main.py status` | Check backend and Tabby reachability |
| `.venv/bin/python3.12 cli/main.py login record "<App>" "<url>"` | Create a login recording session |
| `.venv/bin/python3.12 cli/main.py login list` | List existing login sessions |
| `.venv/bin/python3.12 cli/main.py login export <session_id>` | Analyze session → write bundle JSON |
| `.venv/bin/python3.12 cli/main.py login review <bundle.json>` | Print validation and review items |
| `.venv/bin/python3.12 cli/main.py login register <bundle.json>` | Provision Application + STAGING ServiceProfile in Tabby |
| `.venv/bin/python3.12 cli/main.py login validate <bundle.json>` | Wait for profile to become HEALTHY |
| `.venv/bin/python3.12 cli/main.py login import <session_id>` | Convenience: export + review + register |
| `.venv/bin/python3.12 cli/main.py login import <session_id> --validate` | Convenience: export + review + register + validate |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Backend not running | `.venv/bin/python3.12 cli/main.py start` |
| `Tabby API not reachable` | Confirm Tabby is running; check `TABBY_API_HOST` in `.env` |
| Bundle has generator errors | Re-record with slower, explicit interactions; avoid rapid clicks |
| Used Workflow Recording instead of Login Recording Mode | Discard session; start new `login record` from Step 2 |
| Low selector confidence in review | Re-record; interact with fields one at a time with visible focus |
| Validate timeout (60s) | Check Tabby logs; verify the keepalive URL returns HTTP 200 when authenticated |
| Keepalive URL is redirect-only | Find a URL that loads authenticated content, not a redirect chain |
