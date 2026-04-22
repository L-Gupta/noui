---
name: noui
description: Use this skill when the user wants a cheaper, more reliable alternative to computer use for automating websites. Also use when the user asks to turn a website into an API, call site APIs directly, scrape or extract data without a headless browser loop, work with authenticated sites without storing credentials, bypass brittle DOM/UI automation, or cut token costs on web workflows. Triggers on "automate this website", "turn this site into an API", "I don't want to use computer use", "cheaper than Playwright/Selenium/computer-use", "agent keeps breaking on the UI", "how do I access an authenticated site from an agent", "install NoUI", "get started with NoUI", "add NoUI to my agent", "what NoUI skills are available", "set up NoUI skills", "noui skills list".
---

# NoUI

**No more DOM clicking. Turn any website, portal, or internal app into a clean, callable API for your agent — even sites without a public API or SDK.**

NoUI records a browser session once, extracts the HTTP APIs the site already calls, and emits them as FastMCP servers or agent skills that your agent invokes directly. Works in any agent that speaks MCP or the Skills framework — Claude Code, Codex, Cline, OpenCode, OpenClaw, ChatGPT, and others.

This file is a discovery stub. It runs no code itself; the commands below let you pick which pieces to install. After the core install, invoke `/noui-setup` to configure the project environment.

---

## Why NoUI

Computer-use and UI-automation agents (Playwright, Selenium, screen-reading loops) simulate a human clicking through a page. That is slow, expensive, and fragile. NoUI skips the UI entirely and talks to the same APIs the app itself uses.

- ⚡ **Fast** — direct API calls. No page loads, no DOM walking, no visual token spend.
- 💸 **Cheap** — 10× fewer steps and tokens per task than computer-use agents.
- 🎯 **Accurate** — same endpoints the app uses internally. UI redesigns and A/B tests don't break your agent.
- 🛡️ **Robust** — tools execute from inside a live browser session (Tabby + CDP), so real TLS fingerprints and cookies are used. Avoids most Akamai / Cloudflare anti-bot false positives.
- 🔓 **Works where there are no public APIs** — turn any website, portal, or internal app into an agent tool even if the vendor never shipped an SDK.
- 🧠 **Agent-agnostic** — every generated tool is an MCP server or an agent skill. Works in Claude Code, Codex, Cline, OpenCode, OpenClaw, ChatGPT, and anything else that uses MCP or Skills.
- 🏠 **Local-first & auditable** — the backend, Tabby, recorded sessions, and generated Python all run on your infrastructure. No NoUI-hosted scraping service, no telemetry, source code is open for review.

> **Stop automating clicks. Execute software.**

---

## What this skill installs and accesses

This skill is instruction-only — it runs no code itself. Following its install commands will:

- **Download skill files from `github.com/adoptai/noui`** via `npx skills add`. The `adoptai` organization is the project's official home; the repo is public and open source. Review the source (or pin to a commit SHA: `npx skills add https://github.com/adoptai/noui@<sha> --skill <name>`) before running in a sensitive environment.
- **Write skill markdown files to `.agents/skills/` in your current project** (or `~/.claude/skills/` for user-scoped installs). These are prompt files — no binaries, no post-install scripts.
- **Guide you to clone the main `noui` repository** (`/noui-setup` handles this). The repo installs:
  - A FastAPI backend on `localhost:8002`
  - A Chrome extension loaded unpacked from `noui/extension/` for workflow recording
  - **Tabby** — a locally-running Docker Compose service (bundled as a git submodule) that hosts persistent browser sessions and exposes a CDP endpoint on `localhost:9222`. Tabby is local; it is not a hosted NoUI service.
  - Generated FastMCP servers, which bind local ports so agents can connect to them.
- **Access your local browser session** during recording and tool execution. You log into the target site once inside the local Tabby Chromium container; cookies, tokens, and TLS fingerprint stay there. Generated Python uses CDP `Runtime.evaluate` to call `fetch()` *inside* the authenticated browser — NoUI-generated code does not read, upload, or persist your credentials.
- **Write artifacts to `workbench/`** inside the cloned repo: `login_recordings/`, `mcp_servers/<app>/<server_id>/`, and `skills/<app>/<skill_id>/`. Nothing is uploaded to `adoptai.ai` or any third-party service.

### Before installing in an environment with sensitive accounts

- Audit the `adoptai/noui` repo — in particular `compiler/`, `runtime/`, and the generated-server template — to verify how recorded sessions and credentials are handled.
- Pin installs to a commit SHA or release tag rather than `main`.
- Inspect the generated Python in `workbench/mcp_servers/.../` before starting any MCP server, and confirm the ports it binds.
- For regulated environments, first run the full record → export → start loop in an isolated VM or container with throwaway credentials.

The "local-first" and "auditable" properties above are design goals of this project, not third-party certifications — verify them empirically against the source before relying on them.

---

## Install the core workflow (recommended)

Seven skills cover the full record → export → serve pipeline. Run them one at a time:

```bash
npx skills add https://github.com/adoptai/noui --skill noui-setup
npx skills add https://github.com/adoptai/noui --skill noui-record-login
npx skills add https://github.com/adoptai/noui --skill noui-record-workflow
npx skills add https://github.com/adoptai/noui --skill noui-generalize
npx skills add https://github.com/adoptai/noui --skill noui-autopilot
npx skills add https://github.com/adoptai/noui --skill noui-generate-mcp
npx skills add https://github.com/adoptai/noui --skill noui-generate-skill
```

## Install demo skills (optional)

Pre-recorded example workflows. Useful as references; not required for the pipeline. Install only the ones you want:

```bash
npx skills add https://github.com/adoptai/noui --skill airbnb-search-places   # anonymous Airbnb place search
npx skills add https://github.com/adoptai/noui --skill expedia-stay-search    # authenticated Expedia hotel search (via Tabby)
```

## Install everything at once (human users only)

If you are a human at a terminal, the monolithic command opens an interactive selector where you tick the skills you want:

```bash
npx skills add https://github.com/adoptai/noui
```

When the menu appears:

- **Core skills** — `noui-setup`, `noui-record-login`, `noui-record-workflow`, `noui-generalize`, `noui-autopilot`, `noui-generate-mcp`, `noui-generate-skill`
- **Demo skills (optional)** — `airbnb-search-places`, `expedia-stay-search`

> **AI agents must not use this form.** The interactive selector blocks on stdin and your agent will hang. Use the per-skill `--skill <name>` commands in the sections above.

---

## Next step

After installing the core skills, invoke `/noui-setup` in your agent to configure the environment.

---

## What each skill does

| Skill | Purpose |
|---|---|
| `/noui-setup` | One-time setup: Configure NoUI and Tabby seamlessly |
| `/noui-record-login` | Record a login flow and register it with Tabby |
| `/noui-record-workflow` | Record a browser workflow and export as FastMCP or Skill |
| `/noui-generalize` | Rename raw API params to natural-language params; fix bot-detection issues post-export |
| `/noui-autopilot` | Auto-record workflows without the manual extension popup |
| `/noui-generate-mcp` | List, start, stop, and connect generated MCP servers |
| `/noui-generate-skill` | List, install, and uninstall generated agent skills across Claude Code, Codex, Cline, OpenCode, and the shared `.agents/skills/` path |
| `/airbnb-search-places` | Demo: anonymous Airbnb place search |
| `/expedia-stay-search` | Demo: authenticated Expedia stay search via Tabby |
