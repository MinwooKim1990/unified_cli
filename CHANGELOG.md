# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-06-24

Safety release. No new capabilities — adds Terms-of-Service guardrails and
documentation so the wrapper is harder to misuse in ways that risk an account
ban. Personal, local, individual use with your own subscription remains the
intended pattern; you are responsible for complying with each provider's ToS.

### Security

- **Prominent Terms-of-Service / account-ban warnings** added to the README
  (EN + KO) and usage guides, covering: use-at-your-own-risk, the safe
  personal/local/individual pattern, and the concrete actions that violate
  provider ToS and risk suspension/ban (exposing the server to others / over a
  network, routing other people's requests through your subscription, sharing
  credentials, reselling/proxying access).
- **OpenAI-compatible server now binds to `127.0.0.1` (localhost) by default**
  and **refuses any non-loopback bind unless `UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1`
  is set**, plus logs a personal-use warning on startup. This makes accidental
  network exposure of your personal subscription opt-in rather than the default.

### Changed

- **The `gemini` provider (Antigravity `agy`) is now disabled by default.**
  Automating `agy` can violate Google's Terms of Service, and Google has banned
  individual accounts for it (the ban cascaded across Gemini CLI / Code Assist).
  Enable it at your own risk by setting `UNIFIED_CLI_ENABLE_GEMINI=1`; without
  that env var, `gemini`/`agy` calls raise a config error.

### Added

- `UNIFIED_CLI_ENABLE_GEMINI=1` — opt-in env var to enable the
  Antigravity-backed `gemini` provider.
- `UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1` — opt-in env var to allow the
  OpenAI-compatible server to bind a non-loopback host.

## [0.1.0] - 2026-06-23

Initial public release.

### Added

- **Unified Python API + CLI** over three subscription-authenticated agentic
  CLIs — Claude Code (`claude`), OpenAI Codex (`codex`), and Google Antigravity
  (`agy`). Any subset of the three works; the wrapper shells out to whichever
  CLIs are present and never carries its own credentials.
- **OpenAI-compatible HTTP server** (`unified-cli[server]` extra): drop-in
  `/v1/chat/completions` and `/v1/models` endpoints with model-name auto-routing
  and a `user`-field-as-conversation-id history model.
- **Streaming**: normalized event stream (`text` / `tool_use` / `tool_result` /
  `reasoning` / `usage` / `session` / `done` / `error`) across the three native
  JSONL schemas.
- **Managed multi-turn history** with **cross-provider context injection** — a
  single `UnifiedConversation` can switch providers mid-chat and auto-inject the
  recent turns into the new provider's prompt.
- **Web search** enabled by default (Claude `WebSearch`, Codex `web_search`;
  the `agy`-backed provider decides agentically on its own).
- **Image (multimodal) input** across all three providers, via each CLI's native
  vision path.
- **Dynamic model listing** per provider (Claude models API, Codex local cache,
  `agy models`), with arbitrary model IDs always passed straight through.
- **Structured error classification** (`UnifiedError` across seven categories)
  with automatic auth-expiry fallback to API-key env vars for Claude/Codex.
- **Onboarding wizard** (`unified-cli setup`), **status UI** (`doctor`,
  `status --watch`), and an auto-updating **web dashboard** at `/dashboard`.
- **Interactive REPL** (`unified-cli repl`) with slash commands and
  cross-provider switching.

### Changed

- The `gemini` provider now wraps the **Google Antigravity `agy` CLI** instead
  of the standalone Gemini CLI, which Google blocked for individual accounts in
  2026. The provider key remains `"gemini"` and model slugs such as
  `gemini-3.5-flash` continue to route to it. Note: `agy` headless output is
  plain text and does **not** report token usage.

[Unreleased]: https://github.com/MinwooKim1990/unified_cli/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/MinwooKim1990/unified_cli/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/MinwooKim1990/unified_cli/releases/tag/v0.1.0
