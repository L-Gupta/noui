# NoUI Skills

Skills for the full NoUI workflow — from environment setup to recording, compiling, and running FastMCP servers and agent skills.

NoUI is agent-agnostic: the FastMCP servers it produces work with any MCP-compatible client, and generated skills target Claude Code, Codex, Cline, OpenCode, or the shared `.agents/skills/` convention.

These meta-skills are compatible with any agent that supports the [Skills](https://github.com/vercel-labs/skills) framework — Claude Code, Cursor, Cline, and others.

## Installation

Start with the entry-point skill — it's a discovery guide that lists every other skill with per-skill install commands:

```bash
npx skills add https://github.com/adoptai/noui --skill noui
```

Then invoke `/noui` in your agent and follow the install commands it prints.

Install any specific skill directly:

```bash
npx skills add https://github.com/adoptai/noui --skill noui-setup
```

> **Human users** can run `npx skills add https://github.com/adoptai/noui` (no `--skill` flag) to open an interactive picker. AI agents must use the per-skill `--skill <name>` form — the picker blocks on stdin.

`npx skills add` installs these NoUI meta-skills to `.agents/skills/` in your current project by default; they become available as slash commands immediately. (That's the Vercel Labs convention. **Generated** skills produced by `workflow export --as skill` are a separate thing — they're managed via `noui skill install <id> <agent>` and can target Claude Code, Codex, Cline, OpenCode, or the shared `.agents/skills/` path. See `/noui-generate-skill` for details.)

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
  → workflow export --as mcp <session_id> --capture-session <capture_session_id> --profile <tabby_profile_id>

Path B (unauthenticated / public API):
  start backend → workflow record → Chrome: Start Capture → perform workflow → Stop
  → workflow captures (note capture_session_id)
  → workflow export --as mcp <session_id> --capture-session <capture_session_id>
```

Output: `server_id` → used in `/noui-generalize` or `/noui-generate-mcp`

---

### Phase 3 (alt) — Autopilot Recording

#### `/noui-autopilot`

Fully automated workflow recording: the agent drives a real Chrome browser through the NoUI extension via HTTP commands — no manual clicking through the extension popup required. Reads page state, decides actions, and executes browser commands itself, then compiles the capture into a FastMCP server.

```
start backend → noui autopilot start
→ agent drives browser (get_page_summary → click/type/navigate)
→ noui autopilot stop → noui autopilot export
  (defaults to CDP execution mode; pass --execution-mode http for legacy path)
```

Use when: you want hands-off recording of a site without manually operating the Chrome extension. Prereq: `/noui-setup` complete and Chrome open with the NoUI extension loaded.

Output: `server_id` → used in `/noui-generalize` or `/noui-generate-mcp`

---

### Phase 3.5 — Generalize & Fix Execution

#### `/noui-generalize`

Make generated MCP tools **work** and **usable**. Covers two dimensions:

1. **Execution strategy** — diagnose bot detection (Akamai/Cloudflare 429s), fix Tabby credential_types DB bugs, promote profiles to ACTIVE, HITL login fallback when CloakBrowser fails, and rewrite operations to use CDP browser-side fetch (bypasses TLS fingerprinting).
2. **Interface cleanup** — rename raw API params (`f_sid`, `bl`, `reqid`) to natural-language names (`origin`, `destination`, `departure_date`) so any agent can invoke tools without domain knowledge.

```
Phase 0: Test tool → works? skip to interface cleanup
  ├─ 429 / bot detection → CDP fetch rewrite
  ├─ Empty credentials → fix credential_types DB format
  ├─ No active profile → promote STAGING → ACTIVE
  └─ Login didn't work → HITL login via chrome://inspect

Phase 2-4: Read tools → ask user about workflow → rewrite params one tool at a time
Phase 5: Test, iterate, restart the agent (so it reloads tool schemas)
```

Output: working tools with natural-language interfaces → used in `/noui-generate-mcp`

---

### Phase 4 — MCP Server Management

#### `/noui-generate-mcp`

Start, stop, list, and connect generated FastMCP servers to any MCP-compatible client (Claude Code, Codex, Cline, OpenCode, Cursor, etc.).

```
mcp list                    → see all generated servers
mcp start <server_id>       → spawn server process
mcp status <server_id>      → check running state
mcp stop <server_id>        → stop server
→ Register with your agent's MCP config → restart the agent → tools available
  (Claude Code: ~/.claude.json · Codex/Cline/OpenCode: their respective MCP config)
```

### Phase 4 (alt) — Skill Management

#### `/noui-generate-skill`

List, inspect, install, and uninstall generated agent skills.
Targets Claude Code, Codex, Cline, OpenCode, or the shared `.agents/skills/` convention.
Use when `workflow export` was run with `--as skill` or `--as both`.

```
skill list                           → see all generated skills
skill show <skill_id>                → manifest + SKILL.md preview
skill install <skill_id> <agent>     → install for the target agent
                                       (agents: claude-code, codex, cline, opencode, agents)
                                       (add --project to install into the current project)
skill uninstall <skill_id> <agent>   → remove installed copy
→ The agent loads the skill on demand (restart behavior varies by agent)
```

## CLI Reference

All commands: `.venv/bin/python cli/main.py <command>` from the `noui/` directory.

| Command | Purpose |
|---|---|
| `start` / `stop` / `status` | Backend lifecycle |
| `login record / list / export / review / register / validate / import` | Login session lifecycle |
| `workflow record / list / export` | Workflow session lifecycle |
| `mcp list / start / stop / status` | MCP server lifecycle |
| `skill list / show / install / uninstall` | Generated-skill lifecycle; install/uninstall take `<skill_id> <agent>` (claude-code, codex, cline, opencode, agents) with optional `--project` |
