# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
