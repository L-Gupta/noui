---
name: noui-generalize
description: Use this skill when the user wants to generalize a recorded MCP workflow, rename tools to readable names, replace raw API params with natural-language parameters, fix bot detection issues (Akamai, Cloudflare, PerimeterX), rewrite operations to use CDP browser execution, or make a generated MCP server usable by Claude Code. Triggers on "generalize a recorded workflow", "rename MCP tools", "replace raw API params with readable names", "make the MCP usable by Claude Code", "generalize tool signatures", "I can't use the MCP tools", "Akamai is blocking", "429 with valid cookies", "TLS fingerprinting", "CDP fetch", "execute from inside the browser", "anti-bot workaround", "tabby credentials are empty", or "browser is authenticated but API calls fail".
---

# NoUI Generalize

Take a generated FastMCP server (or Skill) and make it **work** and **usable**:

1. **Execution strategy** — generated servers execute inside the Tabby browser via CDP by default (see `/noui-record-workflow` → *How Execution Works*). This skill handles the edge cases: SPA content that needs navigation + DOM scraping, sites requiring HITL login, profiles that need credential-type fixes, or rare cases where the `--execution-mode http` fallback is the right call.
2. **Interface cleanup** — replace raw internal API parameters (`f_sid`, `bl`, `reqid`) with natural-language names (`origin`, `destination`, `departure_date`) so Claude Code can invoke tools without domain knowledge.

**Prerequisite:** `/noui-record-workflow` must be complete and the MCP server must exist under `workbench/mcp_servers/`. For authenticated sites, `/noui-record-login` must also be complete with a running Tabby session.

---

## Works for both MCP and Skill outputs

The generalization logic is identical for either output format:

- **MCP output:** edit `workbench/mcp_servers/<app>/<server>/operations/<tool>.py` — the body of `async def execute(...)`. The FastMCP tool signature in `server.py` mirrors the execute() signature, so renaming params also requires updating the `server.py` decorator args.
- **Skill output:** edit `workbench/skills/<app>/operations/<tool>.py` — the body of `async def execute(...)` *and* the `_build_parser()` argparse registrations, because the skill operation is a standalone CLI script. The `SKILL.md` body's command examples also reference the flag names, so update them too (or regenerate SKILL.md by re-running `workflow export --as skill --description-override "..."`).

The Phase 0 execution diagnosis (bot detection, empty credentials, profile promotion) applies to both outputs unchanged — both runtimes share the same `noui_runtime/auth.py` and the same Tabby credential flow.

After generalizing a skill, use `/noui-generate-skill` (not `/noui-generate-mcp`) for install / test.

---

## Critical Rules (Never Violate)

- **NEVER** rewrite all tools at once — propose the new name and parameter list for each tool and get user approval before editing any files
- **ALWAYS** preserve existing execution mechanics (URL, method, headers, CDP vs. httpx) unless deliberately switching modes — only the Python function interface changes
- **ALWAYS** hardcode values that were static in the recording (session routing params, build labels, `bl`, `f_sid`, `reqid`, `soc_app`, etc.) — do not expose infrastructure params to the caller
- **NEVER** ask questions that can be answered by reading the code or URL
- `httpx` only appears in generated operations when `--execution-mode http` was used explicitly. Default generated operations use `noui_runtime.cdp.cdp_fetch`. If you see `httpx` in a default-mode server, something is wrong.
- **ALWAYS** connect to the direct CDP port (`localhost:9222`) for `Runtime.evaluate` — the Tabby relay (`localhost:9223`) only allows screencast and input events.
- **ALWAYS** use `credentials: 'include'` in browser-side `fetch()` calls — the generated `cdp_fetch` helper already does this; preserve it when hand-editing.
- **NEVER** assume "Login successful" in worker logs means login actually worked — CloakBrowser reports success when the DSL finishes, not when auth cookies appear. Always verify by checking cookies.
- After rewriting, always remind the user to restart Claude Code to reload the updated tools

---

## Phase 0 — Diagnose Execution Strategy

Before touching tool names or params, check whether the tools actually work.

### 0a. Find and test the server

```bash
ls -lt workbench/mcp_servers/
```

Run a quick end-to-end test:

```bash
.venv/bin/python -c "
import asyncio, json, sys
sys.path.insert(0, 'workbench/mcp_servers/<app_slug>/<server_id>')
from operations.<tool_name> import execute
async def test():
    result = await execute(...)  # fill in sample params
    print(json.dumps(result, indent=2)[:2000])
asyncio.run(test())
"
```

### 0b. Classify the result

| Result | Diagnosis | Next step |
|---|---|---|
| 200 with real data | Tool works — skip to Phase 2 (interface cleanup) |
| `No Tabby page matching '<domain>'` | No tab open on the target site | Open the site in the Tabby browser, or `tabby session ensure --profile <id>` |
| Connection refused on 9222 | Tabby session not running | `tabby session ensure --profile <id>` |
| CORS error / `credentials include not allowed` | Cross-origin fetch blocked | Re-export with `--execution-mode http` |
| 429 / "Too Many Requests" from CDP | Very rare — Akamai flagging in-browser too | Phase 1 — verify the account, consider HITL re-login |
| Server was generated with `--execution-mode http` and returns 429 | httpx blocked by TLS fingerprinting | Re-export without `--execution-mode http` to land on the CDP default |
| Empty credentials / `name: ""` (http mode only) | Tabby credential_types format bug | Phase 1, Fix A |
| `No active profile found` (http mode only) | Profile still STAGING | Phase 1, Fix B |

### 0c. Confirm bot detection (if 429)

Verify the browser itself can make the call:

```python
# Execute fetch from INSIDE the browser via CDP
import asyncio, json, websockets, httpx

async def test():
    resp = await httpx.AsyncClient().get("http://localhost:9222/json", timeout=5)
    targets = resp.json()
    ws_url = [t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page"][0]

    js = """
    fetch('/api/endpoint/test', { credentials: 'include' })
      .then(r => r.text().then(t => JSON.stringify({status: r.status, body: t.substring(0, 200)})))
    """
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": js, "awaitPromise": True, "returnByValue": True}
        }))
        resp = json.loads(await ws.recv())
        print(resp["result"]["result"]["value"])

asyncio.run(test())
```

If 200 from browser but 429 from httpx → **confirmed TLS fingerprinting**. Proceed to Phase 1.

---

## Phase 1 — Fix Execution Strategy

### Fix A — credential_types format bug

After a fresh `login register`, the `service_profiles` table stores `credential_types.cookies` as a string array (`["cookie_a", "cookie_b"]`), but `credentials.service.ts` expects object arrays with `.name`. Every cookie comes back with empty name and value.

```sql
-- Run from tabby/ directory:
-- docker compose exec -T postgres psql -U browser_hitl -d browser_hitl

-- Check current format:
SELECT credential_types FROM service_profiles WHERE profile_id = '<profile>';

-- Fix: convert each string to {name, volatility} object:
UPDATE service_profiles SET credential_types = '{
  "cookies": [
    {"name": "EG_SESSIONTOKEN", "volatility": "STABLE"},
    {"name": "bm_sz", "volatility": "VOLATILE"},
    {"name": "ak_bmsc", "volatility": "VOLATILE"},
    {"name": "user", "volatility": "STABLE"}
  ],
  "headers": []
}'
WHERE profile_id = '<profile>';
```

Mark cookies that rotate frequently (Akamai tokens: `bm_sz`, `ak_bmsc`, `bm_so`, `bm_s`, `_abck`) as `VOLATILE`. Everything else is `STABLE`.

### Fix B — Promote profile to ACTIVE

The credentials API (`POST /credentials/request`) only resolves `ACTIVE` profiles. Fresh registrations start as `STAGING`.

```sql
UPDATE service_profiles SET version_state = 'ACTIVE' WHERE profile_id = '<profile>';
```

### Fix C — Set refresh_interval_seconds

The keepalive runner defaults to 3600s between artifact re-exports. Set lower for faster credential refresh:

```sql
UPDATE applications
SET export_policy = export_policy || '{"refresh_interval_seconds": 60}'::jsonb
WHERE name ILIKE '%<app_name>%';
```

### Fix D — HITL login (when CloakBrowser fails)

If the automated login DSL can't complete (Akamai blocks form interactions), log in manually:

1. Open Chrome → `chrome://inspect`
2. Click "Configure..." → add `localhost:9222`
3. Click **inspect** on the page target
4. In DevTools Console: `window.location = 'https://example.com/login'`
5. Complete login manually via the screencast panel
6. Force re-export:
   ```sql
   UPDATE sessions SET artifacts_last_exported_at = NULL WHERE id = '<session_id>';
   ```

> **Port 9222 vs 9223:** Port 9222 is the direct CDP endpoint (full access). Port 9223 is Tabby's relay that only allows `Page.screencast*` and `Input.dispatch*Event`. Use 9222 for all workaround steps.

### Fix E — CDP execution is the default (no rewrite needed)

CDP-based execution is now the **default** generated output. A recently exported
server already has operations that call `find_page` and `cdp_fetch` from
`noui_runtime/cdp.py`. If you are hand-editing or need to reason about the
pattern, the primitives are:

```python
from noui_runtime.cdp import find_page, cdp_fetch

ws_url = await find_page("api.example.com")           # match the recorded domain
result = await cdp_fetch(                             # fetch inside the browser
    ws_url,
    "https://api.example.com/v1/items",
    method="POST",
    headers={"Accept": "application/json"},
    body={"name": "x"},                               # JSON-encoded automatically
)
```

`cdp_fetch` always sets `credentials: 'include'`. The full specification lives
in `compiler/runtime/cdp_adapter.py`.

**When you still need to re-generate or hand-edit:**

- The server was generated with `--execution-mode http` and keeps getting 429.
  → Re-export without the flag to land on the CDP default.
- You need SPA content that isn't exposed as a JSON API. → see *DOM-scrape
  generalization* below.
- The API truly cannot be reached from the browser origin (CORS, or
  server-to-server endpoint). → keep `--execution-mode http` and fix the
  underlying auth / credential issue instead.

For the rationale (TLS fingerprinting, why 9222, why `credentials: 'include'`),
see `/noui-record-workflow` → *How Execution Works*.

### DOM-scrape generalization (SPA-rendered content)

When data is rendered by client-side JS and not available as a JSON API, the
generated `cdp_fetch` path won't work — you need to navigate and scrape the
rendered DOM. This is a hand-written generalization on top of the CDP default:

```python
import asyncio, json
import websockets

async def _navigate_and_scrape(ws_url: str, url: str, extract_js: str, wait_seconds: int = 7) -> dict:
    """Navigate to a URL, wait for SPA render, then extract data via JS."""
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({
            "id": 1, "method": "Page.navigate", "params": {"url": url}
        }))
        await ws.recv()
    await asyncio.sleep(wait_seconds)
    # Reuse the CDP runtime's eval helper — same session, same cookies
    from noui_runtime.cdp import cdp_eval
    return await cdp_eval(ws_url, extract_js)
```

To find the right DOM selectors, inspect `data-stid` or similar attributes:

```python
# Discovery: list all data-stid values on the page
js = """
JSON.stringify([...new Set(
  [...document.querySelectorAll('[data-stid]')]
    .map(e => e.getAttribute('data-stid'))
)].sort())
"""
```

**What to change when adding DOM-scrape to an operation:**

1. Keep `from noui_runtime.cdp import find_page, cdp_eval` (the default module already exposes both).
2. Add `import asyncio, json, websockets` for `Page.navigate`.
3. Replace the `cdp_fetch(...)` call with `_navigate_and_scrape(ws_url, target_url, extract_js)`.
4. Extract the shape you need via a JS expression that returns `JSON.stringify(...)`.

> **Single browser instance caveat:** Tabby runs one CloakBrowser per session. CDP navigation changes the page — if keepalive actions need the homepage, coordinate or set keepalive to `dom_check` on `body` only.

---

## Phase 2 — Understand the Server

1. Read `tools.json` — note all tool names and their raw parameter names.

2. Read each `operations/<name>.py` — understand the call: URL, method, request body structure, which params are infrastructure vs. business inputs.

3. Ask the user:

> *"What workflow did you record? Describe in plain language what you were doing — for example: 'I searched for flights from Fortaleza to Seattle on June 1 for 1 adult'."*

Use the answer to anchor the business meaning of each tool.

---

## Phase 3 — Close Gaps Per Tool

For each tool, ask only the questions needed to understand which parameters carry business input:

- "Which parameter in this tool controls [the thing you described]? Do you know what value you used during recording?"
- "This tool posts to `[endpoint]` — is this the main [action]? What inputs does it need from the user?"
- For opaque bodies: "The request body appears to be binary-encoded. Based on what you described (origin, destination, date), which of these raw params do you think carries each value?"

Skip questions that are answerable from the URL path, parameter names, or values visible in the code.

---

## Phase 4 — Rewrite Tools One at a Time

For each tool, follow this sequence:

### 4a. Propose

Show the user the proposed new interface before touching any files:

```
Tool: create_data_batchexecute
New name: search_flights
New parameters:
  - origin: str          # IATA code or city name (e.g. "FOR", "Fortaleza")
  - destination: str     # IATA code or city name (e.g. "SEA", "Seattle")
  - departure_date: str  # Date in YYYY-MM-DD format
  - passengers: int = 1  # Number of passengers

Hardcoded (from recording):
  - f_sid: "<value from recording>"
  - bl: "<value>"
  - reqid: <value>
  - soc_app: <value>

Execution: CDP fetch / httpx (state which)

Approve this? (yes / adjust: ...)
```

### 4b. Edit on approval

Once approved, make four edits:

1. **`operations/<old_name>.py`** — rewrite the function signature and body:
   - New function name matches the approved tool name
   - New parameters are natural-language (`origin`, `destination`, etc.)
   - Infrastructure params are hardcoded as local variables
   - Build the raw request from the natural params (string formatting, encoding)
   - Use CDP fetch if Phase 1 flagged bot detection; otherwise preserve the existing HTTP call

2. **`tools.json`** — update the entry:
   - `name` → new tool name
   - `description` → plain-English description of what it does
   - `parameters` → updated list with natural-language names, types, descriptions

3. **`server.py`** — update the import and tool registration to use the new function name (if the function was renamed)

4. **`API.md`** — refresh the documentation:

```bash
.venv/bin/python cli/main.py mcp docs <server_id>
```

This overwrites `API.md` from the current `tools.json`. Run it after every tool edit, not just at the end.

### 4c. Move to next tool

Repeat Phase 4 for each tool. Do not batch edits.

---

## Phase 5 — Iterate After Testing

After all tools are rewritten, do a final docs refresh:

```bash
.venv/bin/python cli/main.py mcp docs <server_id>
```

Then tell the user:

> "Done. `API.md` is up to date. Please restart Claude Code (close and reopen, or run `/reconnect`) to reload the updated tools. Then try invoking the workflow — for example: 'search for flights from Fortaleza to Seattle on June 1'."

When the user reports results, fix any issues:

| Problem | Fix |
|---|---|
| Wrong parameter mapping | Re-read the operation, ask clarifying question, re-propose |
| Missing required parameter | Add it to signature and body |
| Tool call fails with HTTP error | Read the error body, compare to original recording values |
| Body encoding wrong | Check if original body was URL-encoded, JSON, or protobuf — reconstruct accordingly |
| 429 from tool call on a CDP-default server | Verify Tabby has an authenticated tab open; if yes, the account may be flagged — redo HITL login (Phase 1, Fix D) |
| 429 from tool call on `--execution-mode http` server | Re-export without the flag to use the CDP default |
| CDP connection refused | Tabby session not running — `tabby session ensure --profile <id>` |
| `fetch()` returns 403 inside browser | Session expired — redo HITL login (Phase 1, Fix D) |

Keep iterating until the user can successfully invoke the workflow using natural language.

---

## What to Hardcode vs. Expose as Parameters

| Hardcode | Expose as parameter |
|---|---|
| `f_sid`, `bl`, `reqid`, `soc_app` | Origin, destination, dates |
| Build labels, session routing tokens | Search inputs (keywords, quantities) |
| CSRF tokens captured during recording | Filters (cabin class, stops) |
| App version identifiers | User preferences the tool is supposed to accept |
| Infrastructure headers (`x-goog-ext-*`) | Any value that changes meaningfully per call |

When in doubt: if the value was the same every time during recording and doesn't carry user intent, hardcode it.

---

## Known Anti-Bot Sites

| Site | Bot Detection | CloakBrowser Login | Workaround |
|---|---|---|---|
| Expedia | Akamai Bot Manager | Form blocked silently; reaches homepage but can't submit | HITL login + CDP fetch + DOM scrape |
| (add more as discovered) | | | |

---

## Architecture Notes

- **Why cookies alone fail:** Akamai fingerprints the TLS ClientHello, HTTP/2 settings frame, header order, and other transport-layer signals. `httpx`/`curl` have distinctly different fingerprints from Chrome, even with identical cookies.
- **Why CDP works:** `Runtime.evaluate` + `fetch()` executes inside the real Chrome process. The HTTP request goes through Chrome's network stack with its native TLS implementation and fingerprint.
- **Port 9222 vs 9223:** Port 9222 is the direct CDP endpoint (full access). Port 9223 is Tabby's relay proxy that only allows `Page.startScreencast`, `Page.stopScreencast`, `Page.screencastFrameAck`, and `Input.dispatch*Event`. Use 9222 for this workaround.
- **Single browser instance:** Tabby runs one CloakBrowser per session. CDP navigation changes the page for that session — if keepalive needs the homepage, set keepalive health checks to `dom_check` on `body` rather than URL-based checks.

---

## Decision Flow

```
Start
  │
  Phase 0: Test the tool as-is
  │
  ├─ Works (200 with data)?
  │     └─ Skip to Phase 2 (interface cleanup)
  │
  ├─ "No Tabby page matching <domain>"?
  │     └─ Open the site in Tabby, or `tabby session ensure --profile <id>`
  │
  ├─ CORS / cross-origin error?
  │     └─ Re-export with `--execution-mode http`
  │
  ├─ 429 on a default-mode server?
  │     └─ Phase 1: Fix D (HITL re-login) — the session or account is flagged
  │
  ├─ 429 on an `--execution-mode http` server?
  │     └─ Re-export without the flag to land on the CDP default
  │
  ├─ Empty credentials / name: "" (http mode)?
  │     └─ Phase 1: Fix A (credential_types format)
  │
  ├─ "No active profile" (http mode)?
  │     └─ Phase 1: Fix B (promote to ACTIVE)
  │
  ├─ Login didn't actually work?
  │     └─ Phase 1: Fix D (HITL login)
  │
  Phase 2: Read tools.json + operations + ask user about workflow
  │
  Phase 3: Close gaps per tool (targeted questions only)
  │
  Phase 4: Rewrite tools one at a time (propose → approve → edit)
  │
  Phase 5: Test, iterate, remind user to restart Claude Code
```

---

## Related Skills

- `/noui-record-login` — Record login and register with Tabby (run first for authenticated sites)
- `/noui-record-workflow` — Record and export the workflow (run first to generate the server)
- `/noui-generate-mcp` — Server lifecycle after generalization (start/stop/connect to Claude Code)
