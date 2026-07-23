# Extensions changelog

This file records changes for the extension source tree bundled in `unified-cli`.
It is not the changelog of an independently published package.

## [Unreleased]

### Planned for unified-cli 0.5.1

- Consolidates the extension source into the single `unified-cli` distribution;
  the wheel will expose both `unified_cli` and `unified_cli_ext` namespaces.
  Core defaults remain Claude, Codex, and Gemini only, while extensions remain
  explicit and lazy. Grok stays Preview; Qoder, Kilo, and Poolside are runnable
  Experimental integrations; and the other 14 providers stay Held. All Ext
  server policies stay disabled.
- Moves optional protocol installs to `unified-cli[acp]` and
  `unified-cli[mcp]`. No extension-specific PyPI project or GitHub Release is
  planned.

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
- Adds Held catalog metadata for Kimi, Copilot, Cursor, CodeBuddy, Mistral
  Vibe, Qwen, Cline, OpenCode, Factory Droid, Pi, Oh My Pi, Hermes Agent, Amp,
  and GitLab Duo CLI. These 14 entries remain unavailable until
  provider-specific compatibility evidence is completed and reviewed. Grok is
  a runnable Preview only through the explicit exact setup documented above;
  Qoder, Kilo Code, and Poolside Agent CLI are runnable Experimental
  integrations. All remain server-disabled, and none is Stable.
- Separates registry lifecycle from integration support status and generates
  the public support table from plugin metadata with a CI consistency check.
- Keeps extension identifiers explicit and lazy. Installing Ext does not change
  Core defaults or the Core server allowlist, handle provider credentials, or
  call a provider service.
- The `ext-v0.1.0` tag was an aborted publishing attempt. It did not publish an
  extension package to public PyPI and did not create a GitHub Release. The
  planned 0.5.1 unified distribution supersedes that split-release plan.

[Unreleased]: https://github.com/MinwooKim1990/unified_cli/compare/v0.5.0...HEAD
[0.1.0]: https://github.com/MinwooKim1990/unified_cli/tree/ext-v0.1.0
