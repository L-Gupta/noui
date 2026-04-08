---
name: noui-generalize
description: Use this skill when the user wants to generalize a recorded MCP workflow, rename tools to readable names, replace raw API params with natural-language parameters, or make a generated MCP server usable by Claude Code. Triggers on "generalize a recorded workflow", "rename MCP tools", "replace raw API params with readable names", "make the MCP usable by Claude Code", "generalize tool signatures", or "I can't use the MCP tools".
---

# NoUI Generalize

Take a generated FastMCP server with raw internal API parameters and rewrite it to use natural-language parameters (`origin`, `destination`, `departure_date`, etc.) so Claude Code can invoke the tools without domain knowledge.

**Prerequisite:** `/noui-record-workflow` must be complete and the MCP server must exist under `mcp_servers/`.

---

## Critical Rules (Never Violate)

- **NEVER** rewrite all tools at once — propose the new name and parameter list for each tool and get user approval before editing any files
- **ALWAYS** preserve existing HTTP mechanics (URL, method, headers, auth pattern) — only the Python function interface changes
- **ALWAYS** hardcode values that were static in the recording (session routing params, build labels, `bl`, `f_sid`, `reqid`, `soc_app`, etc.) — do not expose infrastructure params to the caller
- **NEVER** ask questions that can be answered by reading the code or URL
- After rewriting, always remind the user to restart Claude Code to reload the updated tools

---

## Phase 1 — Understand the Server

1. Find the server directory. Look in `mcp_servers/` for the most recently modified server, or ask the user which server to generalize:

```bash
ls -lt mcp_servers/
```

2. Read `tools.json` — note all tool names and their raw parameter names.

3. Read each `operations/<name>.py` — understand the HTTP call: URL, method, request body structure, which params are infrastructure vs. business inputs.

4. Ask the user:

> *"What workflow did you record? Describe in plain language what you were doing — for example: 'I searched for flights from Fortaleza to Seattle on June 1 for 1 adult'."*

Use the answer to anchor the business meaning of each tool.

---

## Phase 2 — Close Gaps Per Tool

For each tool, ask only the questions needed to understand which parameters carry business input:

- "Which parameter in this tool controls [the thing you described]? Do you know what value you used during recording?"
- "This tool posts to `[endpoint]` — is this the main [action]? What inputs does it need from the user?"
- For opaque bodies: "The request body appears to be binary-encoded. Based on what you described (origin, destination, date), which of these raw params do you think carries each value?"

Skip questions that are answerable from the URL path, parameter names, or values visible in the code.

---

## Phase 3 — Rewrite Tools One at a Time

For each tool, follow this sequence:

### 3a. Propose

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

Approve this? (yes / adjust: ...)
```

### 3b. Edit on approval

Once approved, make four edits:

1. **`operations/<old_name>.py`** — rewrite the function signature and body:
   - New function name matches the approved tool name
   - New parameters are natural-language (`origin`, `destination`, etc.)
   - Infrastructure params are hardcoded as local variables
   - Build the raw request from the natural params (string formatting, encoding)
   - Preserve the actual HTTP call (httpx/requests, headers, auth)

2. **`tools.json`** — update the entry:
   - `name` → new tool name
   - `description` → plain-English description of what it does
   - `parameters` → updated list with natural-language names, types, descriptions

3. **`server.py`** — update the import and tool registration to use the new function name (if the function was renamed)

4. **`API.md`** — refresh the documentation to reflect the change:

```bash
.venv/bin/python cli/main.py mcp docs <server_id>
```

This overwrites `API.md` from the current `tools.json`. Run it after every tool edit, not just at the end.

### 3c. Move to next tool

Repeat Phase 3 for each tool. Do not batch edits.

---

## Phase 4 — Iterate After Testing

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
