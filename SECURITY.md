# Security Policy

## Reporting a vulnerability

If you discover a security issue in NoUI, please report it privately. Do **not**
open a public GitHub issue.

**Contact:** gabriel@adopt.ai

Please include:

- A description of the issue and its impact.
- Steps to reproduce, including the affected version or commit SHA.
- Any proof-of-concept code or requests (redact credentials and customer data).

We will acknowledge your report within 5 business days and keep you informed of
our progress toward a fix.

## Scope

In scope:

- The NoUI backend (`backend/`)
- The NoUI CLI (`cli/`)
- The NoUI Chrome extension (`extension/`)
- The MCP runtime and compiler (`runtime/`, `compiler/`)

Out of scope:

- The pinned Tabby dependency — report Tabby issues upstream at
  https://github.com/adoptai/tabby.
- Third-party services that generated MCP servers call into.

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.x     | Yes       |
| < 1.0   | No        |

## Handling sensitive data in issues

Do not include real credentials, session cookies, HAR payloads from authenticated
sessions, or customer-identifiable data in public issues or PRs. If a reproduction
requires sensitive material, email the maintainer contact above.
