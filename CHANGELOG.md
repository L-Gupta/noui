# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Default execution mode for generated MCP servers and Skills is now CDP
  browser execution** (`--execution-mode cdp`). Generated operations open a
  WebSocket to Tabby's CDP endpoint (`localhost:9222`), locate the tab for the
  recorded domain, and call `fetch(url, {credentials: 'include'})` via
  `Runtime.evaluate` — cookies and TLS fingerprint come from the real
  authenticated browser. Sidesteps Akamai / Cloudflare false positives that
  fire on Python HTTP clients.
- `auth.execution_strategy` added to `manifest.json` (additive, non-breaking).
  `"cdp_browser_session"` under the default; mirrors `auth.strategy` under
  `--execution-mode http`. Existing readers of `auth.strategy` are unaffected.
- `/noui-record-workflow` skill now documents the CDP default under *How
  Execution Works*; `/noui-generalize` reframed around hand-edit cases on top
  of the default (SPA DOM scraping, HITL login).
- Compiler now rejects empty, malformed, or no-API HARs early with
  `HarValidationError` instead of silently generating MCP servers or Skills
  with zero tools. The workflow export endpoint (`POST /workflow/.../export`)
  returns `422 Unprocessable Entity` with the validation message for these
  cases and reserves `500` for unexpected bugs. No output artifacts
  (`server.py`, `tools.json`, `SKILL.md`, `manifest.json`) are written on
  failure.
- CLI now loads the repo-root `.env` (matching `backend/config.py`) so
  `TABBY_API_HOST` set in `.env` is honoured by every `noui` subcommand
  instead of being silently ignored. Values without a scheme (e.g.
  `localhost:8080`) are normalised to `http://localhost:8080` and trailing
  slashes are stripped, eliminating opaque `urlopen` failures.

### Added

- `compiler/runtime/cdp_adapter.py` — generates `noui_runtime/cdp.py` in every
  compiled output, exposing `find_page`, `cdp_eval`, `cdp_fetch`.
- `--execution-mode {cdp,http}` flag on `noui workflow export` and
  `noui autopilot export`; corresponding query param on the backend export
  endpoint.
- Explicit `httpx` and `websockets` runtime dependencies in `pyproject.toml`
  (were previously transitive).
- 18 new tests covering CDP-default invariants and the `--execution-mode http`
  opt-in for both MCP and Skill outputs.

### Notes

- Existing generated servers under `workbench/mcp_servers/` are **not**
  rewritten. Re-exporting an old recording will produce CDP-based output;
  pass `--execution-mode http` to reproduce the legacy shape.
- `auth.strategy` is unchanged and remains the credential-source descriptor
  (`tabby_credentials` / `static_secret_header`). Only execution mechanics
  changed, not credential classification.

## [1.0.0] - 2026-04-17

Initial open-source release.

### Added

- Public MIT license.
- `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`.
- Tabby pinned as a git submodule tracking the `tabby-noui` branch at SHA
  `d212467`. NoUI's CLI now resolves `TABBY_DIR` to the in-repo submodule by
  default; set `TABBY_DIR` to point at a sibling checkout.
- Issue and pull-request templates under `.github/`.
- Dependabot configuration for `pip` and `github-actions`.
- Secrets-scan and extension-lint jobs in CI.
- Chrome extension: configurable backend URL via popup Settings and
  `homepage_url` in the manifest.

### Changed

- Unified version to `1.0.0` across `pyproject.toml`, `extension/manifest.json`,
  and this changelog.
- Replaced internal fixture references (`adopt-bank` → `example-bank`).

[Unreleased]: https://github.com/adoptai/noui/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/adoptai/noui/releases/tag/v1.0.0
