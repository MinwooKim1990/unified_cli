# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-07-22

### Added

- A versioned provider-extension ABI and lazy entry-point registry. Core keeps
  Claude, Codex, and Gemini as its only built-in defaults; installed extension
  metadata is enumerated passively and extension code loads only after an
  explicit provider request.
- A local-only management API and dashboard bootstrap with one-time secrets,
  host-only sessions, origin-scoped CSRF proof, bounded chat relay, explicit
  provider verification, and no implicit model/provider probes.
- Durable, security-reviewed REPL state and richer slash-command workflows,
  including settings, session indexing, prompt/history controls, and bounded
  local exports.
- A deterministic offline performance/readiness gate covering Core and Ext
  imports, version startup, passive registry enumeration, real-PTY first prompt,
  management bootstrap, bounded relay, and fixture-only wrapper overhead.

### Changed

- Provider subprocess ownership, cancellation, process-tree cleanup, output
  limits, and concurrent stream isolation are hardened across synchronous and
  asynchronous paths.
- Provider turns now retry only clearly pre-turn transient network failures or
  transient HTTP 429 responses, with bounded `Retry-After` handling,
  exponential backoff with jitter, strict attempt/delay caps, and
  cancellation-aware waits. Once output or tool execution may have begun, the
  turn is never replayed.
- Authentication/authorization failures, quota exhaustion, and policy denials
  are never retried. The wrapper no longer changes to an inherited API key and
  replays a failed OAuth turn; recovery hints direct users to login or to make a
  new, explicitly metered Python request via `extra_env`.
- Core and `unified-cli-ext` are built and verified as separate distributions;
  Ext 0.1.x requires the released Core 0.5.x compatibility line.
- Release automation now requires immutable version tags at the exact current
  `main` commit, validates wheel/sdist filenames, roots, RECORD integrity,
  SHA-256 member hashes, file/directory hierarchy, the complete default-runtime
  dependency set, optional-extra markers, and Core/Ext dependency boundaries.
  The Ext gate also re-downloads the final Core GitHub Release, requires exact
  asset sizes and SHA-256 digests, and revalidates both artifact bytes before
  publishing.

### Security

- Core fast paths do not import optional provider packages, enumerate entry
  points, inspect real provider installations, inherit credential variables, or
  contact a network service.
- Offline readiness checks record and fail caught extension-entry-point imports
  or provider subprocess attempts; only the repository fixture executable is
  permitted during the wrapper-overhead comparison.
- Management mode remains loopback-only and browser chat stays restricted to
  audited Core mappings; extension server access is denied by default.

## [0.4.0] - 2026-07-14

### Security

- The OpenAI-compatible server now keeps Codex and `agy` disabled by default,
  runs Claude with a constrained server profile, and rejects unsafe HTTP image
  inputs before provider execution.
- External server binding now requires both an explicit opt-in and a strong
  Bearer token; malformed or missing authentication fails closed.
- Provider processes now use bounded output, timeouts, process-group cleanup,
  temporary-file scopes, and configurable resource limits so a failed or
  hostile CLI invocation cannot grow without bound.

### Added

- `unified-cli config default-provider [claude|codex|gemini]` and
  `--reset`, plus `unified-cli --version`.
- Saved CLI/REPL sessions now restore a valid working directory, while an
  explicit `--cwd` always takes precedence.

### Changed

- CI now tests every declared Python version through 3.14, validates built
  distributions, and installs the wheel in a clean environment before release.
- The PyPI workflow accepts only a matching `vX.Y.Z` tag already on `main`,
  then runs tests, package metadata checks, and a clean-wheel smoke test.
- GitHub Actions are pinned to immutable commit SHAs and Dependabot checks for
  GitHub Actions updates weekly.

## [0.3.0] - 2026-07-03

Reliability, headless/daemon robustness, and security release — the outcome of a
comprehensive adversarial audit. The headline fix makes the wrapper safe to run
from **launchd / cron / a server** (the context where `claude` silently hung on
the macOS Keychain), plus a class of streaming-subprocess correctness fixes, a
hardened localhost server, and REPL/CLI UX polish. Every phase was verified by an
independent third-party audit pass.

### Fixed

- **Streaming no longer hangs forever when a wrapped CLI wedges before output.**
  All streaming paths (sync `stream()`, async `astream()`, and the `agy` path)
  now read the child on a background thread/task with an **output watchdog**: if
  the CLI produces no first line within `first_output_timeout` (~60s) or goes
  idle past `stream_timeout`, the child is killed and an actionable error is
  raised. Previously the timeout sat in a `finally` after a blocking read and was
  unreachable — the REPL/server could wedge indefinitely. The watchdog measures
  the **child's** output cadence, so a slow consumer (SSE backpressure) never
  kills a healthy child.
- **macOS Keychain / launchd diagnosis.** When `claude` hangs in a non-interactive
  context with credentials only in the login Keychain, the error now names the
  cause and the fix (`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`).
- **`stdin=DEVNULL`** on all streaming subprocess spawns — a child that reads
  stdin can no longer hang in a daemon/piped context.
- **API key no longer leaks into the child by default.** `_env()` now strips an
  inherited `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` unless the auth-expired
  fallback needs it, so an exported key can't silently switch you from your
  subscription to metered API billing. (The old fallback was also a no-op.)
- **UTF-8 subprocess I/O** (`encoding="utf-8", errors="replace"`) everywhere;
  `astream()` no longer crashes on stdout lines over 64 KB.
- **Server: localhost enforced at the ASGI layer.** A middleware rejects
  non-loopback peers and non-loopback `Host` headers (DNS-rebinding defense,
  using `ipaddress` — not a `127.` string prefix), so the invariant holds even
  under a raw `uvicorn ...:app --host 0.0.0.0`. Opt out with
  `UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1`.
- **Server: `CONVS` is now a bounded LRU** (max 200) with lock-guarded access;
  anonymous single-turn requests are no longer stored — fixes unbounded growth
  and an `OrderedDict mutated during iteration` race under concurrency.
- **Setup wizard honors the gemini gate** — it no longer spawns the `agy` OAuth
  flow (install/login/verify) unless `UNIFIED_CLI_ENABLE_GEMINI=1`.
- **Conversation stream: session id / turn preserved on early stop.** A consumer
  that stops mid-stream (Ctrl+C, SSE disconnect) no longer loses the native
  session id or the partial turn.
- Readline history file/dir hardened to `0o600`/`0o700` **before** first write
  (no world-readable window). A prompt beginning with `-` (e.g. `--version`) is
  now passed as text via a `--` sentinel, not parsed as a flag.

### Added

- **Headless binary discovery.** `claude`/`codex` are now probed in well-known
  install locations (`/opt/homebrew/bin`, `~/.local/bin`, npm-global, …) when not
  on `PATH` — fixes "binary not found" under launchd's minimal `PATH`.
- **`unified-cli doctor --headless`** — a real per-provider preflight (tiny call,
  short timeout, closed stdin) you run *from your service context* to prove auth
  works there instead of discovering a hang at runtime. `doctor` also recognizes
  `CLAUDE_CODE_OAUTH_TOKEN` and reports a blocked Keychain.
- **`unified-cli serve`** — first-class subcommand for the dashboard + OpenAI API
  (`--port`, `--open`), always bound to loopback.
- **REPL session resume** — `unified-cli repl --continue` / `-c` and a `/resume`
  slash command reopen your last saved session; a stale session is recovered
  automatically.
- **Pipe-friendly `chat`** — diagnostics, spinners, and the session panel go to
  **stderr**, so `unified-cli chat "…" | jq` gets clean model output on stdout.
  A missing prompt on an interactive terminal now shows usage instead of blocking.
- A **"Running under launchd / cron / a server"** section in the README + USAGE
  (English + Korean).

### Changed

- **Faster startup for `--version` / `--help` / `doctor` / `chat`** — the heavy
  `prompt_toolkit` / `rich.progress` imports are now lazy (loaded only by `repl`
  / `setup`).
- CI matrix expanded to Python 3.9–3.13 on Linux plus macOS (3.9 & 3.13);
  added `Environment :: Console` / `Typing :: Typed` / Python 3.14 classifiers.

## [0.2.0] - 2026-06-24

REPL UX, internationalization, and dashboard release, hardened by a 9-round
adversarial audit. The interactive REPL is now a first-class, discoverable
surface (live slash-command menu, model/provider pickers, live status), the
whole CLI/REPL is localized (English default + Korean), and the web dashboard
was redesigned. `prompt_toolkit` becomes a core dependency, so a plain
`pip install unified-cli` ships the full REPL out of the box.

### Added

- **prompt_toolkit-powered interactive REPL** (`unified-cli repl`):
  - **Live slash-command menu** — type `/` to get an as-you-type dropdown of
    every slash command (in a real terminal).
  - **Model picker** — `/model` with no argument opens a picker listing each
    provider's latest models, with the default marked ★, so you never have to
    hand-type a model name. `/model <name>` still works (multi-word `agy`
    display names supported). `/provider` likewise opens a picker.
  - **Live `/status` panel** inside the REPL (auto-refreshing; Ctrl+C returns
    you to the prompt).
  - Localized `/help`. Falls back to a plain `input()` loop (with the same
    commands) when stdin/stdout is not a TTY.
- **Internationalization (i18n), English default + Korean.** The entire
  CLI/REPL is localized. Resolution order: `--lang {en,ko}` flag >
  `~/.unified-cli/settings.json` > `$UNIFIED_CLI_LANG` > default English. New
  global `--lang` flag and `UNIFIED_CLI_LANG` env var; in the REPL, `/lang ko`
  and `/lang en` switch language live and persist the choice.
- **Redesigned web dashboard** (`/dashboard`): quick-stat cards, per-provider
  health cards, inline-SVG sparklines (latency / token volume), per-model usage
  bars, and a responsive layout.
- **`http://127.0.0.1:PORT/` now redirects to `/dashboard`** (previously a 404).

### Changed

- **`prompt_toolkit` is now a core dependency.** A plain
  `pip install unified-cli` now includes the full interactive REPL — there is
  **no `[repl]` extra**. The optional extras remain `server`, `dev`, and `all`.
- **The default CLI language is English.** Use `--lang ko` (or `/lang ko` in
  the REPL, or `UNIFIED_CLI_LANG=ko`) for Korean.

### Fixed

- **Rich-markup-safe terminal output.** Untrusted model names, file paths, and
  raw CLI output can no longer corrupt or crash the display via Rich markup.
- **Streaming subprocess robustness.** Fixed an stderr-pipe deadlock and ensure
  the child process is killed on abort — for sync, async, and the `agy`-backed
  provider.
- **`--terse` no longer crashes the REPL on non-Claude providers.**
- **`status --watch-interval` now validates its input** instead of accepting
  bad values.

### Security

- **The `gemini`/`agy` provider's model listing now respects the
  `UNIFIED_CLI_ENABLE_GEMINI` ToS gate** — it will not spawn `agy` when gated
  (e.g. from `doctor` or the dashboard).
- **The REPL prompt-history file is now created with `0o600` permissions.**
- **The dashboard model chart is prototype-pollution-safe.**

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

[Unreleased]: https://github.com/MinwooKim1990/unified_cli/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/MinwooKim1990/unified_cli/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/MinwooKim1990/unified_cli/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/MinwooKim1990/unified_cli/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/MinwooKim1990/unified_cli/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/MinwooKim1990/unified_cli/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/MinwooKim1990/unified_cli/releases/tag/v0.1.0
