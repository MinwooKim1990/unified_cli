# Changelog

All notable changes to `unified-cli-ext` are documented in this file.

## 0.1.0 — 2026-07-20

Initial extension-foundation release for `unified-cli` 0.5.x.

- Adds bounded JSONL, JSON-RPC, HTTP/SSE, process, normalization, permission,
  tool-correlation, and optional MCP/ACP runtime contracts.
- Adds a caller-factory-free ACP 0.11 text-turn transport with fixed process
  ownership, cancellation, output limits, deterministic cleanup, and exact
  notification/response event ordering.
- Adds immutable local installation receipts for direct executables and scoped
  npm launchers. Receipts bind inspected local files; they do not establish
  publisher identity.
- Adds inert Held catalog metadata for Grok, Kimi, Copilot, Cursor, CodeBuddy,
  Qoder, Mistral Vibe, Qwen, Cline, OpenCode, Kilo Code, Factory Droid, Pi,
  Oh My Pi, Hermes Agent, and Poolside Agent CLI. These entries
  remain unavailable until provider-specific compatibility evidence is
  completed and reviewed.
- Separates registry lifecycle from integration support status and generates
  the public support table from plugin metadata with a CI consistency check.
- Keeps extension identifiers explicit and lazy. Installing Ext does not change
  Core defaults or the Core server allowlist, handle provider credentials, or
  call a provider service.
