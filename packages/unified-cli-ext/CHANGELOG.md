# Changelog

All notable changes to `unified-cli-ext` are documented in this file.

## [Unreleased]

## [0.1.0] - 2026-07-23

- Records a representative isolated device-code Grok native smoke: official
  `0.2.111` on macOS arm64, verified 2026-07-23. It covers direct chat,
  streaming/usage mapping, sessions and resume, invalid-model handling,
  cancellation, cleanup, and the public Core configuration path. The provider
  remains Preview and server-disabled; this is not broad compatibility evidence.

- Adds a server-disabled Grok Build Preview with fixed read-only agent tools,
  bounded official-CLI probes, streaming JSONL/session normalization, and
  offline regressions for malformed output, cancellation, and output limits;
  it requires exactly Grok `0.2.111`, fixes managed-MCP and gitignore-aware
  controls, requires an exact private safe config, and fails closed on project,
  provider-home, and managed system configuration. These controls are defense
  in depth, not a complete secret boundary. It is not Stable.
- Refreshes the Kimi Code CLI target to 0.29.0 while keeping Kimi, GitHub
  Copilot CLI, Cursor Agent CLI, and the other 14 catalog entries Held.

Initial extension-foundation release for `unified-cli` 0.5.x.

- Adds bounded JSONL, JSON-RPC, HTTP/SSE, process, normalization, permission,
  tool-correlation, and optional MCP/ACP runtime contracts.
- Adds a lazy macOS libc compatibility path for non-reaping child observation
  when the running Python does not expose `os.waitid`.
- Adds a caller-factory-free ACP 0.11 text-turn transport with fixed process
  ownership, cancellation, output limits, deterministic cleanup, and exact
  notification/response event ordering.
- Adds immutable local installation receipts for direct executables and scoped
  npm launchers. Receipts bind inspected local files; they do not establish
  publisher identity.
- Adds inert Held catalog metadata for Kimi, Copilot, Cursor, CodeBuddy, Qoder,
  Mistral Vibe, Qwen, Cline, OpenCode, Kilo Code, Factory Droid, Pi, Oh My Pi,
  Hermes Agent, Poolside Agent CLI, Amp, and GitLab Duo CLI. These 17 entries
  remain unavailable until provider-specific compatibility evidence is
  completed and reviewed. Grok is the sole runnable Preview, only through the
  explicit exact setup documented above; it remains server-disabled and is not
  Stable.
- Separates registry lifecycle from integration support status and generates
  the public support table from plugin metadata with a CI consistency check.
- Keeps extension identifiers explicit and lazy. Installing Ext does not change
  Core defaults or the Core server allowlist, handle provider credentials, or
  call a provider service.
- Ships independently from the exact same source commit as Core 0.5.0, after
  Core is available from PyPI. The Ext release verifies both published Core and
  Ext versions, the exact default-runtime dependency set, and archive integrity.
  Before Ext testing begins it also downloads the final Core GitHub Release,
  verifies the exact asset set, sizes, SHA-256 digests, and artifact bytes, then
  revalidates both Core archives. Its mandatory `ext-v0.1.0` GitHub Release is
  created with the verified Ext wheel and sdist attached.

[Unreleased]: https://github.com/MinwooKim1990/unified_cli/compare/ext-v0.1.0...HEAD
[0.1.0]: https://github.com/MinwooKim1990/unified_cli/releases/tag/ext-v0.1.0
