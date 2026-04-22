---
name: noui
description: Use this skill whenever the user wants to automate a website, interact with a web app, or build a tool on top of a site — especially when they want a cheaper, faster, and more reliable alternative to computer-use or browser-clicking agents. Also use when the user asks to turn a website into an API, call site APIs directly, scrape or extract data without a headless browser loop, work with authenticated sites without storing credentials, bypass brittle DOM/UI automation, or cut token costs on web workflows. Triggers on "automate this website", "turn this site into an API", "I don't want to use computer use", "cheaper than Playwright/Selenium/computer-use", "agent keeps breaking on the UI", "how do I access an authenticated site from an agent", "install NoUI", "get started with NoUI", "add NoUI to my agent", "what NoUI skills are available", "set up NoUI skills", "noui skills list".
---

# NoUI

**The secure, robust, and accurate way to access any website — without computer use.**

NoUI turns any website into a clean, callable API for your agent. Record a session once, and NoUI extracts the real APIs the site already uses and ships them as MCP tools your agent calls directly. No clicking, no DOM scraping, no brittle browser loops.

This file is a discovery stub — the commands below let you install the pieces you actually want. It does not install anything on its own. After the core install, invoke `/noui-setup` to configure the project environment (venv, deps, `.env`, Chrome extension).

---

## Why NoUI

Computer-use and UI-automation agents (Playwright, Selenium, screen-reading loops) simulate a human clicking through a page. That is slow, expensive, and fragile. NoUI skips the UI entirely and talks to the same APIs the app itself uses.

- ⚡ **Fast** — direct API calls. No page loads, no DOM walking, no visual token spend.
- 💸 **Cheap** — 10× fewer steps and tokens per task than computer-use agents.
- 🎯 **Accurate** — same endpoints the app uses internally. UI redesigns and A/B tests don't break your agent.
- 🛡️ **Robust** — tools execute from inside a live browser session (via Tabby + CDP), so real TLS fingerprints and cookies are used. No Akamai / Cloudflare false positives, no anti-bot blocks.
- 🔐 **Secure** — credentials never leave the browser. NoUI does not extract, store, or transmit passwords or tokens; the agent reuses an authenticated session you logged into once.
- 🧠 **Agent-native** — every generated tool is an MCP server or a Claude Code Skill. Works with Claude, ChatGPT, Codex, Cline, OpenClaw, and anything else that speaks MCP.

> **Stop automating clicks. Execute software.**

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

After installing the core skills, run `/noui-setup` in Claude Code to configure the environment (venv, deps, `.env`, Chrome extension).

---

## What each skill does

| Skill | Purpose |
|---|---|
| `/noui-setup` | One-time setup: venv, deps, `.env`, Chrome extension |
| `/noui-record-login` | Record a login flow and register it with Tabby |
| `/noui-record-workflow` | Record a browser workflow and export as FastMCP or Skill |
| `/noui-generalize` | Rename raw API params to natural-language params; fix bot-detection issues post-export |
| `/noui-autopilot` | Auto-record workflows without the manual extension popup |
| `/noui-generate-mcp` | List, start, stop, and connect generated MCP servers |
| `/noui-generate-skill` | List, install, and uninstall generated Claude Code Skills across agents |
| `/airbnb-search-places` | Demo: anonymous Airbnb place search |
| `/expedia-stay-search` | Demo: authenticated Expedia stay search via Tabby |
