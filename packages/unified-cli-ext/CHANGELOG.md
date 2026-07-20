# Changelog

All notable changes to `unified-cli-ext` are documented in this file.

## 0.1.0 — 2026-07-20

Initial Stage 2 foundation release.

- Adds extension transport and contract foundations for `unified-cli` 0.5.x.
- Keeps extension identifiers explicit and lazy; installing this distribution
  does not modify Core defaults or the Core server allowlist.
- Ships no functional provider adapters, authentication flows, or live
  provider integrations.
- Provides fake/offline validation only; no provider calls, paid-service calls,
  credential scraping, authentication bypass, or rate-limit bypass.
- Documents optional ACP and MCP v1 SDK boundaries without making either a
  mandatory provider integration.
