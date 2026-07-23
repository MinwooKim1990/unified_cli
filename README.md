# unified-cli

**One Python + CLI interface for Claude Code, OpenAI Codex, Google
Antigravity, Grok, and 17 more coding-agent CLIs.**

[![PyPI version](https://img.shields.io/pypi/v/unified-cli)](https://pypi.org/project/unified-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/unified-cli)](https://pypi.org/project/unified-cli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

🇰🇷 [한국어 README](README.ko.md) · 📖 [Detailed usage (EN)](USAGE.md) · 📖 [상세 가이드 (한국어)](USAGE.ko.md)

## Start here

Choose **one** installation path. Both install the same Core + Preview provider
code; there is no separate `unified-cli-ext` download.

### A. Install from PyPI — use `unified-cli` from any directory

`pipx` is recommended for a terminal application because it gives
`unified-cli` its own environment while placing the command on your global
`PATH`:

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
# Open a new terminal once if ensurepath asks you to.
pipx install "unified-cli[server,acp]"

unified-cli --version
unified-cli providers --include-ext
```

Use `pipx upgrade unified-cli` for later releases. If you are already inside a
Python virtual environment, ordinary pip is also fine:

```bash
python -m pip install "unified-cli[server,acp]"
```

The base package is enough for the REPL. `server` adds the browser/local HTTP
server; `acp` adds the optional ACP transport used by Qoder, Kilo, Hermes, and
Poolside (Python 3.10–3.14).

### B. Run a Git checkout — develop or test the current source

```bash
git clone https://github.com/MinwooKim1990/unified_cli.git
cd unified_cli
python3 -m venv .venv
source .venv/bin/activate                  # Windows: .venv\Scripts\activate
python -m pip install -e ".[server,acp,dev]"

unified-cli --version                     # runs this checkout
unified-cli repl
```

The command is available while that virtual environment is active. To keep an
editable checkout but expose its command globally, run this once from the
repository root instead:

```bash
pipx install --force --editable ".[server,acp]"
```

### Primary use: embed any provider in Python

The terminal and browser are optional front ends. The primary API is the same
small Python wrapper for all three Core providers and all 18 Preview providers:

```python
from pathlib import Path

from unified_cli import PROVIDERS, UnifiedError, configure_extension_provider, create

provider = "grok"  # or claude, codex, gemini, kimi, copilot, qwen, ...
workspace = str(Path.cwd().resolve())  # Preview providers require an absolute path

# Recommended once after installing/upgrading the vendor CLI. This verifies its
# executable and stores a local launch receipt; it does not perform vendor login.
if provider not in PROVIDERS:
    configure_extension_provider(provider)

client = create(provider, cwd=workspace)
try:
    response = client.chat("Explain this project")
    print(response.text)
except UnifiedError as error:
    print(error.kind, error)  # e.g. auth_expired, rate_limit, config
```

Streaming and async use the same object:

```python
for event in client.stream("Review the current diff"):
    if event.kind == "text":
        print(event.text, end="", flush=True)

# In an async function:
# response = await client.achat("Explain this project")
# async for event in client.astream("Review the diff"): ...
```

`unified_cli_ext` is bundled in the same wheel and loaded lazily. Application
code should normally import the stable public API from `unified_cli`; no second
PyPI package or subprocess-sidecar is needed. See
[`examples/09_extensions.py`](examples/09_extensions.py) for a runnable example.
Non-Python language bindings are future work; the supported embedded contract
in 0.5.3 is the Python API above.

### What to run after either installation

```bash
# 1. Passive catalog: shows Core + all bundled Preview providers; runs no vendor CLI.
unified-cli providers --include-ext

# 2. Check installed binaries/login state when you explicitly ask.
unified-cli doctor

# 3. Verify and save an installed Preview CLI once (example: Grok).
unified-cli configure grok

# 4. Full-screen terminal UI: choose provider/model and type / for commands.
unified-cli repl

# 5. One command without entering the REPL.
unified-cli chat "explain this repository" --provider claude --cwd "$PWD"
unified-cli chat "explain this repository" --provider grok --cwd "$PWD"

# 6. Local browser management UI. Only the listed workspace can be used.
unified-cli serve --manage --workspace "$PWD" --open
```

The terminal prints the one-time local browser URL if `--open` cannot launch
it. Plain `unified-cli serve --open` shows the read-only dashboard;
`--manage --workspace ...` enables local provider/model/settings/chat controls.
Keep it on `127.0.0.1`; the public-compatible `/v1/*` API retains its stricter
Core-only trust boundary.

All 18 bundled Preview adapters are exposed through the same Python and
terminal APIs and are attempted only when explicitly selected. In the
credential-free 2026-07-23 lab, current official installations reached
`create()` for 13 providers. Cursor, Hermes, Mistral Vibe, and Qoder returned
bounded compatibility errors, while Poolside was not installed because its
installer required EULA acceptance. See the
[accountless lab evidence](docs/development/ext-accountless-live-lab-2026-07-23.md).
Browser chat additionally requires a fixed read-only boundary; this release
allows explicit attempts for
`grok`, `copilot`, `qoder`, `mistral-vibe`, `qwen`, `kilo`, `pi`,
`oh-my-pi`, `hermes`, and `poolside`. The other Preview entries still appear
with a clear reason and remain selectable through `create()`/REPL/CLI.

> **Prerequisites — this package installs and authenticates _nothing_.**
> `unified-cli` is a thin wrapper that shells out to the official agentic CLIs
> you already have. It ships **no API keys and no credentials**, and it
> **stores or transmits no credentials of its own**. Stable Core calls reuse
> the vendor login already on your machine; Preview providers may ask you to
> repeat the vendor's official login inside their isolated provider home.
>
> Before using a provider you must have installed the corresponding CLI **and
> signed in with your own subscription**:
>
> - **Claude** → the `claude` CLI (Claude Code), logged in with Claude Pro/Max
> - **Codex** → the `codex` CLI, logged in with ChatGPT Plus/Pro
> - **Gemini** → the `agy` CLI (Google Antigravity), logged in with your Google
>   Antigravity account
>
> **Any subset works** — you do not need all three. The wrapper simply uses
> whichever of `claude` / `codex` / `agy` it finds on your `$PATH`.

## Supported CLIs at a glance

The normal `unified-cli` install includes Core and Extensions together. There is
no second `unified-cli-ext` package to install.

| Status | Supported coding CLIs (Provider ID) | What it means |
|---|---|---|
| **Stable Core** | Claude Code (`claude`), OpenAI Codex (`codex`), Google Antigravity (`gemini` / `agy`) | Existing behavior and defaults remain unchanged |
| **Preview — executable adapter, attempted when selected** | Grok Build (`grok`), Kimi Code (`kimi`), GitHub Copilot CLI (`copilot`), Cursor Agent (`cursor`), CodeBuddy (`codebuddy`), Qoder (`qoder`), Mistral Vibe (`mistral-vibe`), Qwen Code (`qwen`), Cline (`cline`), OpenCode (`opencode`), Kilo Code (`kilo`), Factory Droid (`droid`), Pi (`pi`), Oh My Pi (`oh-my-pi`), Hermes Agent (`hermes`), Poolside Agent CLI (`poolside`), Amp (`amp`), GitLab Duo CLI (`gitlab-duo`) | All adapters have shared transport fixtures; the current accountless lab reached `create()` for 13/18, with five limitations documented above |

Preview does **not** mean “catalog only.” Every listed Preview provider has an
executable adapter and is attempted when you explicitly select it:

```bash
unified-cli providers --include-ext
unified-cli configure grok
unified-cli chat "explain this project" --provider grok --cwd "$PWD"
unified-cli chat "review this change" --provider kimi --cwd "$PWD"
unified-cli chat "find the bug" --provider copilot --cwd "$PWD"
```

ACP-based Preview providers (`qoder`, `kilo`, `hermes`, `poolside`) require
Python 3.10+ and:

```bash
pip install "unified-cli[acp]"
```

Install and sign in to the corresponding official vendor CLI first. Extensions
are lazy: they are never auto-selected, never change the Core default provider,
and remain disabled on the public-compatible `/v1/*` routes. The loopback-only
`serve --manage` UI can run an Ext provider only after you explicitly select it
and register an absolute workspace. Preview processes use a private provider
home; if a vendor stores login only in its normal home, repeat that vendor's
official login in the private home described in the
[extension guide](https://github.com/MinwooKim1990/unified_cli/blob/main/docs/extensions.md).
Grok uses the guide's verified isolated login setup.

> **Preview compatibility notice:** Grok has a representative authenticated
> live test. The other Preview integrations reuse tested protocol families and
> may need updates for a particular vendor version, account, or output schema.
> A failed Preview run writes a bounded, prompt-free diagnostic under
> `~/.unified-cli/preview-diagnostics/`. Please attach that file to a
> [GitHub issue](https://github.com/MinwooKim1990/unified_cli/issues/new).
> Reports intentionally omit prompts, environment values, auth data, and tokens.

Python uses the same installed package and registry. `create()` is the public
embedded API; importing `unified_cli_ext` directly is optional:

```python
from pathlib import Path
from unified_cli import configure_extension_provider, create

configure_extension_provider("grok")
client = create("grok", cwd=str(Path.cwd().resolve()))
print(client.chat("Explain this project").text)
```

See [Extensions](https://github.com/MinwooKim1990/unified_cli/blob/main/docs/extensions.md)
for provider-specific install commands, Preview limitations, and protocol details.

<a id="provider-usage-policy"></a>

## Terms of Service & provider usage policy — read before using

> **You are responsible for complying with each provider's Terms of Service.**
> Automation may not be permitted for every account or use case, and service
> access may be restricted. Terms are evolving (clarified Feb 2026); this is
> not legal advice.

- **Intended safe pattern = personal, local, individual use with your OWN
  subscription.** Anthropic officially supports headless `claude -p` /
  programmatic use, so that path is lower risk. Never expose the wrapper to
  other people.
- **Do NOT:** run the OpenAI-compatible server on a public/network interface,
  route other people's requests through your subscription, share credentials,
  or resell/proxy access. These may conflict with provider policies and can
  result in service access being restricted.
- **Antigravity (`agy` / the `gemini` provider) requires additional policy
  review.** Google has reported access restrictions for individual accounts
  that automate it, including related Gemini CLI / Code Assist access. For
  that reason the `gemini` provider is **disabled by default** — enable it
  only after reviewing the applicable policy by setting
  `UNIFIED_CLI_ENABLE_GEMINI=1`.
- **The `unified-cli serve` and `python -m unified_cli.server` launchers bind to
  `127.0.0.1` (localhost) by default** and **refuse a non-loopback host unless**
  you set `UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1`. A raw `uvicorn` command keeps
  Uvicorn's own host choice, but the app's ASGI guard returns HTTP 403 for a
  non-loopback bind, peer, or Host until that opt-in. External opt-in additionally
  requires a non-whitespace `UNIFIED_CLI_SERVER_AUTH_TOKEN` of at least 32 UTF-8
  bytes and a matching `Authorization: Bearer …` header on every request. This is
  for one trusted client behind TLS, not a way to create a public or multi-user
  proxy.
- This package ships **no credentials** — each user brings their own
  subscription, and nothing is stored or transmitted on your behalf.

Use all three AI coding CLIs — each signed in with your personal subscription
(Claude Pro/Max, ChatGPT Plus/Pro, Google Antigravity) — from a single unified
interface, both as a **terminal CLI** and as a **Python library you can
`import` in your own code**.

> The provider key for the Google side is still `"gemini"` (and `-m
> gemini-3.5-flash` etc. still route to it), but it now wraps the **Antigravity
> `agy` CLI** — access to the old `gemini` CLI was restricted for individual
> accounts in 2026. See the migration note below.
>
> ⚠️ **The `gemini` provider is disabled by default** because automating `agy`
> can result in Google service access restrictions. Set
> `UNIFIED_CLI_ENABLE_GEMINI=1` only after reviewing the applicable policy — see
> [Terms of Service & provider usage policy](#provider-usage-policy).

```bash
# CLI
$ unified-cli chat "hi" -m haiku
# or: unified-cli repl  →  interactive mode with slash commands
```

```python
# Python
from unified_cli import create, UnifiedConversation
resp = create("claude").chat("hi")
conv = UnifiedConversation()
conv.send("Hello", provider="claude")
conv.send("Continue", provider="gemini")   # needs UNIFIED_CLI_ENABLE_GEMINI=1
```

> The `gemini` provider is **disabled by default** (Antigravity `agy` automation
> can result in Google service access restrictions). Export
> `UNIFIED_CLI_ENABLE_GEMINI=1` before any `gemini` example below will work.

## Why this exists

Each of the three CLIs (`claude`, `codex`, `agy`) ships great subscription
auth but lives in its own world. Want to route "quick query" to the fastest
model regardless of provider? Want a local OpenAI-compatible `/v1/chat/completions`
endpoint with a constrained Claude default (and an explicit external-sandbox
opt-in for agentic providers)? Want your Python app to switch providers
mid-conversation with automatic context handoff? That's what
this wrapper does — **as a CLI you can shell into, and as a Python package you
can import**.

## Features

- **Dual mode**: full-featured CLI (`unified-cli chat`, `repl`, `status`, ...)
  AND clean Python API (`from unified_cli import ...`) — same code, same state
- **Subscription-aware**: uses your existing `claude` / `codex login` / `agy`
  OAuth. Inherited vendor API keys are stripped, and authentication failures
  never replay a turn under a different credential
- **Multi-turn history**: CLI via `--continue` / `--resume`, Python via
  `session_id=` or `UnifiedConversation`
- **Cross-provider conversation**: one `UnifiedConversation` can switch providers
  mid-chat; the last 8 turns auto-inject as context into the new provider's prompt
- **Unified streaming events**: `kind="text" | "tool_use" | "tool_result" |
  "reasoning" | "usage" | "session" | "done" | "error"` — normalized across
  the three native JSONL schemas
- **Web search by default**: Claude `WebSearch`, Codex `web_search`. The
  `gemini` provider (now the Antigravity `agy` CLI) is agentic and decides
  when to web-search on its own — always available.
- **Image input** (multimodal, all 3 providers): pass `images=[paths]` to
  `chat()` / `stream()` or `--image foo.png` on the CLI. Each provider uses
  its native vision path:
  - **Codex** — `-i, --image <FILE>` flag (codex CLI 0.129+).
  - **Gemini (`agy`)** — `@<path>` reference embedded in the prompt. Tool
    approvals stay enabled unless the caller explicitly opts into the risky
    `skip_permissions=True` mode.
  - **Claude** — Routed through Claude Code's built-in `Read` tool; the image
    path is prepended to the prompt. The wrapper does not automatically select
    `bypassPermissions`. PNG / JPEG / GIF / WebP are supported.
- **Structured errors**: every failure → `UnifiedError(kind=...)` from one of
  eight categories (`auth_expired` / `rate_limit` / `model_not_allowed` /
  `not_found` / `network` / `resource_limit` / `config` / `internal`) with
  recovery hints
- **OpenAI-compatible server**: drop-in `/v1/chat/completions` + redesigned
  auto-updating dashboard at `/dashboard` (and `/` redirects there). Its safe
  default exposes a constrained Claude profile only.
- **Rich terminal UI**: `doctor` health table, `status --watch` live dashboard,
  `setup` interactive wizard, streaming spinner
- **Interactive REPL** (`unified-cli repl`): live `/` slash-command menu,
  `/model` and `/provider` snapshot pickers (default marked ★),
  snapshot-only `/status`, cross-provider switching — powered by `prompt_toolkit`
- **Localized (i18n)**: English by default, Korean with `--lang ko` (or
  `/lang ko` in the REPL, or `UNIFIED_CLI_LANG=ko`)

## Default models (lightweight, subscription-friendly)

| Provider | Default | Latest flagship (override with `-m`) |
|---|---|---|
| Claude | `claude-haiku-4-5` | `claude-fable-5`, `claude-opus-4-8`, `claude-sonnet-5` |
| Codex | `gpt-5.4-mini` | `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` |
| Gemini (`agy`) | `gemini-3.5-flash` | `gemini-3.1-pro` |

Override via `-m <name>`. The wrapper passes any model ID straight through to
the underlying CLI; `/model` in the REPL and the browser Refresh action query
the selected provider explicitly, while `unified-cli models --refresh` does the
same from a command. For the ultra-fast coding specialist currently advertised
by the installed catalog, use `-m gpt-5.3-codex-spark`.

Model discovery is explicit and uses a one-hour, monotonic in-process cache;
imports, server startup, management bootstrap, and REPL startup do not populate
it. Cache and flight keys retain only SHA-256 context fingerprints: normalized
Claude credentials plus proxy/TLS inputs, the canonical Codex HOME/cache-file
identity, or Gemini opt-in/PATH/override plus passive `agy` metadata. No binary
is executed to build a fingerprint. Same-context concurrent refreshes share one
probe. Context entries use LRU bounds of eight per provider and 24 globally;
active refreshes are bounded to four per provider and 12 globally, with a
retryable `resource_limit` error when full. Use
`list_models(provider, force_refresh=True)` or `unified-cli models --refresh`
to refresh explicitly, and `invalidate_model_cache(provider)` (or no argument
for all built-ins) to discard cached records. Returned `ModelInfo` objects are
copies, so caller mutation never changes later results.

> **Gemini → Antigravity migration**: As of 2026, Google restricted the old
> `gemini` CLI for individual accounts (`IneligibleTierError: ... migrate to
> the Antigravity suite`). The `gemini` provider now wraps the **Antigravity
> `agy` CLI** (`~/.local/bin/agy`). `agy` is fully agentic (web search,
> shell, file tools) and routes to several model families — run
> `unified-cli models gemini` (which calls `agy models`) to see them, e.g.
> `Gemini 3.5 Flash (Medium)`, `Gemini 3.1 Pro (High)`,
> `Claude Sonnet 4.6 (Thinking)`, `GPT-OSS 120B (Medium)`. Both the display
> names and slugs like `gemini-3.5-flash` work with `-m`. Unknown names
> silently fall back to the default. Note: `agy` headless mode outputs plain
> text (no token-usage reporting).
>
> ⚠️ **Disabled by default.** Because automating `agy` can lead to Google
> service access restrictions, the `gemini` provider only activates when
> `UNIFIED_CLI_ENABLE_GEMINI=1` is set. Without it, direct `gemini`/`agy` calls
> (and the `gemini-*` model examples above) raise a config error. The HTTP server
> is stricter still: it returns HTTP 403 for Gemini until its separate agentic
> provider opt-in is enabled inside an external sandbox. Review the applicable
> provider policy before enabling direct use.

## Install from source (development)

Follow **Start here → B** above. After activating `.venv`, these are useful:

```bash
unified-cli setup     # optional Core onboarding
unified-cli doctor
python -m pytest
```

Requires Python 3.9+ (Python 3.10–3.14 for the optional ACP extra) and at least
one vendor CLI to make a real call. The setup wizard only suggests official
Core install/login commands; Preview CLI installation remains explicit and is
documented in the [extension guide](docs/extensions.md). It never stores
credentials and every step can be declined.

## Usage at a glance

### CLI

```bash
# Single turn
unified-cli chat "explain python list reversal in one line"

# Continue the last conversation
# Restores its provider/model and a still-valid saved working directory.
# An explicit --cwd always wins.
unified-cli chat "what about in-place?" --continue
unified-cli chat "use this checkout instead" --continue --cwd ~/work/project

# Persist the provider used when no -m/--provider or saved session chooses one
unified-cli config default-provider codex
unified-cli config default-provider            # inspect
unified-cli config default-provider --reset    # return to Claude

# Print just the installed package version (automation-friendly)
unified-cli --version

# Resume a specific session
unified-cli chat "continue from earlier" --resume <session_id>

# Interactive REPL — type `/` for a live menu (/model & /provider pickers, /status, /lang, ...)
unified-cli repl
unified-cli repl --provider exact-extension-id --model vendor/family/model

# Stream + web-search (both defaults)
unified-cli chat "latest Python release?" --stream

# Cheapest fast query
unified-cli chat "quick q" -m gpt-5.3-codex-spark

# Exact extension selection; --model stays literal even with slashes
unified-cli chat "hello" --provider exact-extension-id --model vendor/family/model

# Image input (works with all 3 providers — see Features above for details)
unified-cli chat "what's in this photo?" --image cat.png -m haiku
unified-cli chat "compare these two" --image a.jpg --image b.jpg -m gpt-5.4-mini

# Status & dashboard
unified-cli doctor          # one-time health check
unified-cli status --watch  # live terminal dashboard (5s refresh)
uvicorn unified_cli.server:app --port 8000  # localhost-only by default → http://localhost:8000/dashboard (/ redirects there)
```

### Interactive REPL — `unified-cli repl`

The REPL is powered by `prompt_toolkit` (a core dependency, so it works
straight from `pip install unified-cli`). In a real terminal, type `/` to get a
**live as-you-type menu** of every slash command — you don't have to memorize
them.

```text
[claude/haiku] > hello
[claude/haiku] > /                         # live dropdown of all slash commands
[claude/haiku] > /model                    # Core cache/fallback or loaded Ext snapshot (default ★)
[claude/sonnet] > /provider                # picker: choose a provider (context auto-injected)
[codex/gpt-5.4-mini] > /status             # process-local snapshot; no provider probe
[codex/gpt-5.4-mini] > /lang ko            # switch the UI to Korean (persists)
[codex/gpt-5.4-mini] > /image photo.png    # attach image for the next turn
[codex/gpt-5.4-mini] > describe this
[codex/gpt-5.4-mini] > /save               # current session_id + resume hint
[codex/gpt-5.4-mini] > /exit               # state saved → `chat --continue` from here
```

- **`/model`** with no argument opens a picker. Core uses its in-memory
  cache/fallback; an explicitly loaded extension shows only its descriptor
  default and last successful `/model --refresh` snapshot. `/model <literal>`
  sets the literal model ID without probing.
- **`/provider <exact-id>`** loads only that extension's metadata. The picker
  shows Core plus extension descriptors already loaded in this process.
- **`/status`** shows a process-local snapshot and never probes providers.
- **`/doctor`** shows the existing Core health table when Core is selected. For
  a selected extension it calls only that extension's explicit doctor and
  renders only a Core-owned generic result.
- **`/lang en` / `/lang ko`** switches the UI language live and persists it.

Slash commands: `/help` `/model` `/provider` `/status` `/lang` `/new` `/save`
`/history` `/tokens` `/doctor` `/image` `/images` `/clear-images` `/exit`
(`/quit` alias).
When stdin/stdout isn't a TTY, the REPL falls back to a plain `input()` loop
with the same commands.

### Language (English default, Korean optional)

The whole CLI/REPL is localized. English is the default; switch to Korean with
the global `--lang` flag, the `UNIFIED_CLI_LANG` env var, or `/lang ko` in the
REPL:

```bash
unified-cli --lang ko chat "안녕"          # one-off, Korean output
export UNIFIED_CLI_LANG=ko                  # whole shell session in Korean
```

Resolution order: `--lang {en,ko}` > `~/.unified-cli/settings.json` (set by
`/lang`) > `$UNIFIED_CLI_LANG` > English.

### Python

```python
from unified_cli import create, UnifiedConversation, UnifiedError, load_last_session

# Pattern 1 — single call
resp = create("claude").chat("hi")

# Pattern 2 — external code manages history (typical for chatbots)
cli = create("codex")
sessions = {}
def reply(user_id: str, prompt: str) -> str:
    r = cli.chat(prompt, session_id=sessions.get(user_id))
    sessions[user_id] = r.session_id
    return r.text

# Pattern 3 — wrapper manages history + cross-provider
conv = UnifiedConversation()
conv.send("My name is Minwoo.", provider="claude")
conv.send("What's my name?", provider="gemini")   # knows "Minwoo"

# Pattern 4 — resume from CLI session
state = load_last_session()   # reads ~/.unified-cli/state.json
if state:
    resp = create(state.provider, model=state.model).chat(
        "follow-up from REPL", session_id=state.session_id,
    )

# Pattern 5 — error-aware fallback
for p in ("claude", "codex", "gemini"):
    try:
        return create(p).chat("...")
    except UnifiedError as e:
        if e.kind in ("auth_expired", "rate_limit"):
            continue
        raise

# Pattern 6 — image input (works on all 3 providers)
resp = create("claude").chat(
    "What single color is this image?",
    images=["/path/to/photo.png"],
)
print(resp.text)
# Direct Python/CLI image inputs are trusted local paths (str or pathlib.Path),
# raw bytes, or Attachment(path=...)/Attachment(bytes_=...). Remote URLs and
# data URIs are deliberately rejected by the wrapped CLIs: download or decode
# trusted data yourself before passing it to the wrapper.
images = [
    "cat.png",
    b"\\x89PNG...",                                  # bytes
]
# CLI equivalent:
#   unified-cli chat "describe" --image a.png --image b.jpg -m gpt-5.4-mini
```

See [USAGE.md](USAGE.md) (English) or [USAGE.ko.md](USAGE.ko.md) (Korean) for
the full cookbook — 9 patterns including sync, async, streaming, tool events,
error fallback, image input, CLI↔Python state sharing, and advanced provider
options.

### OpenAI-compatible server

```bash
unified-cli serve --port 8000 --open          # ← recommended: localhost-guarded, opens the dashboard
# Raw ASGI mode uses Uvicorn's host setting; its default is localhost and the
# app rejects non-loopback HTTP requests unless external mode is explicitly enabled.
uvicorn unified_cli.server:app --port 8000
# Browse:  http://localhost:8000/dashboard    (live usage / sessions)
#          http://localhost:8000/             (redirects to /dashboard)
```

> **Localhost-only by default.** `unified-cli serve` and
> `python -m unified_cli.server` bind `127.0.0.1` and **refuse a non-loopback
> host** (e.g. `0.0.0.0`) unless you set `UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1`.
> Raw `uvicorn ... --host 0.0.0.0` can still open a listener, but the app's ASGI
> guard returns HTTP 403 for that non-loopback bind, peer, or Host until the same
> opt-in is set. It also logs a personal-use warning on startup. Exposing your
> personal subscription to other people / over a network can violate provider
> terms and lead to service-access restrictions, so keep it local.

> **External mode is not a public-service mode.** If an independently managed
> deployment must bind outside loopback, it needs both
> `UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1` and a non-whitespace
> `UNIFIED_CLI_SERVER_AUTH_TOKEN` of at least 32 UTF-8 bytes. Every route then
> requires `Authorization: Bearer <token>`, including diagnostics. Use a TLS
> reverse proxy and a single trusted client; a Bearer token provides neither
> HTTPS nor per-user isolation. The browser dashboard is intended for local use.

The opt-in management dashboard never verifies providers or loads models during
bootstrap. Those probes begin only after the corresponding explicit action.
Within that runtime, successful version/auth and non-empty model results use
separate five-minute/15-second/one-minute TTLs. Same-context model misses share
one Manage flight; explicit invalidation and shutdown fence both the Manage and
Core model generations. A forced verification queued behind an ordinary one
waits for it, after which concurrent forced callers share one new generation;
different providers remain independent. Version/auth entries are keyed by the
exact executable selected from `PATH`; Gemini model entries fingerprint the
effective `agy` selected by Core discovery, including `AGY_CLI_PATH`. Claude
models use the HTTP API and Codex models use `~/.codex/models_cache.json`, so
those two model paths execute no CLI and have no fabricated binary identity.
Auth and model data are additionally isolated by hashed HOME/provider-
environment context. Executable identity is local invocation/canonical-target
metadata, not vendor/package provenance. The verifier API also has no provider
account identifier, so an account changed by an external process may remain
visible for at most the short auth TTL. Observed binary replacement invalidates
all of that provider's probe records.

> **HTTP trust boundary.** By default the server accepts only Claude models,
> using Claude safe mode with no agent tools for text requests and a scoped
> read permission for supplied image bytes. Codex and Antigravity (`agy`) are
> intentionally rejected because their agentic CLIs do not provide
> confidential-data isolation for arbitrary HTTP input. Set
> `UNIFIED_CLI_SERVER_ALLOW_AGENTIC_PROVIDERS=1` only in an independently
> sandboxed container or VM with an intentionally scoped workspace mount; it
> is not an authentication mechanism or a safe way to expose the server.

Claude model names are auto-routed; the `user` field acts as a conversation id
(preserves history across calls):

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")

# Plain text turn
client.chat.completions.create(
    model="haiku",                              # → claude
    messages=[{"role":"user","content":"hi"}],
    user="session-1",
)

# Image input (OpenAI multi-content schema, Claude server profile)
client.chat.completions.create(
    model="haiku",                              # → claude
    messages=[{"role":"user","content":[
        {"type":"text","text":"describe"},
        {"type":"image_url",
         "image_url":{"url":"data:image/png;base64,iVBOR..."}}
    ]}],
)
```

For the intentionally restricted external mode, pass the same bearer token as
the OpenAI SDK API key (and keep the endpoint behind TLS):

```python
import os
client = OpenAI(base_url="https://trusted.example/v1",
                api_key=os.environ["UNIFIED_CLI_SERVER_AUTH_TOKEN"])
```

For HTTP images, `image_url.url` must be one canonical base64 URI such as
`data:image/png;base64,...`, `data:image/jpeg;base64,...`,
`data:image/gif;base64,...`, or `data:image/webp;base64,...`, whose signature
matches its MIME type. Remote URLs and filesystem paths are rejected. Defaults
are four images per message, 4 MiB decoded per image,
and a 24 MiB request body; operators can lower or raise those explicit server
limits with `UNIFIED_CLI_SERVER_MAX_IMAGES`,
`UNIFIED_CLI_SERVER_MAX_IMAGE_BYTES`, and
`UNIFIED_CLI_SERVER_MAX_BODY_BYTES`.

## Running under launchd / cron / a server (headless)

The wrapped CLIs are designed to run **interactively**. Under a background
launcher (macOS **launchd**, **cron**, **systemd**, a long-running server
process) two things bite:

**1. Minimal `PATH` → "binary not found".** launchd/cron start with a bare
`PATH` (`/usr/bin:/bin:/usr/sbin:/sbin`), so `claude`/`codex` installed in
Homebrew, npm-global, or `~/.local/bin` aren't found. unified-cli now also
probes the well-known install locations, but the robust fix is to be explicit:

```bash
export CLAUDE_CLI_PATH=/opt/homebrew/bin/claude   # or ~/.local/bin/claude
export CODEX_CLI_PATH=/opt/homebrew/bin/codex
# launchd plist: set these under <key>EnvironmentVariables</key>.
```

**2. macOS Keychain → silent hang.** On macOS, `claude` stores its OAuth
credentials in the **login Keychain**. A launchd/daemon context has **no TTY to
unlock the Keychain**, so the CLI blocks forever waiting on auth — the call
appears to hang and then times out. Works in your terminal, dies only on the
server. Fix it with a **long-lived token** (the officially supported headless
path):

```bash
claude setup-token                         # run ONCE in a real terminal
# → copy the token into your service environment:
export CLAUDE_CODE_OAUTH_TOKEN=<token>     # OAuth-equivalent, NOT metered
```

> By default the wrapper runs on your **subscription OAuth** and **strips any
> inherited `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`** from the child env, so an
> exported key can't silently switch you to per-token billing. Set
> `CLAUDE_CODE_OAUTH_TOKEN` for headless auth. If you intentionally want a
> metered call, make a **new Python request** and pass the key explicitly:

```python
from unified_cli import create

metered = create(
    "claude", extra_env={"ANTHROPIC_API_KEY": "<key-from-secret-store>"},
)
metered.chat("new request")
```

The wrapper never retries a failed OAuth turn with this credential.

**Prove it before you ship.** Run the preflight **from the same context** as
your service (e.g. inside the launchd job) — it makes a tiny real call per
provider and reports whether auth actually works there instead of hanging:

```bash
unified-cli doctor --headless
# ✓ claude: auth OK in this context     → good to go
# ✗ claude: network — ... Keychain ...   → set CLAUDE_CODE_OAUTH_TOKEN
```

Streaming calls also have a short **first-output watchdog**: if a provider
produces no output within ~60s (the classic wedged-on-Keychain case) the wrapper
kills it and returns an actionable error naming the Keychain fix, rather than
blocking indefinitely. `codex` needs no Keychain (`~/.codex/auth.json`); `agy`
uses browser OAuth and stays gated regardless.

## Known limitations

**Speed**: every call spawns a fresh subprocess (`claude -p` / `codex exec` /
`agy` for the `gemini` provider) — these CLIs don't support a long-lived
daemon. Measured latency:

| Stage | Claude | Codex | Gemini |
|---|---|---|---|
| Subprocess spawn | ~50 ms | ~60 ms | ~460 ms (Node bundle) |
| API round-trip (API round-trip) | 3–6 s | 2–3 s | 3–4 s |
| **Full chat turn** | **5–6 s** | **2.7–3 s** | **3–4 s** |

For the absolute fastest interactive feel, use `-m gpt-5.3-codex-spark`. Even
then, expect 2–3 seconds per turn. This is a **structural limit of the
subprocess architecture** — not something the wrapper can fix without either
(a) losing subscription auth by calling provider APIs directly, or (b) using
experimental daemon modes (e.g. `codex app-server`) that aren't fully stable
yet.

**Subscription ToS**: each provider's terms forbid reselling/exposing your
personal subscription as a third-party service. This wrapper is designed for
**personal local automation**, not as a SaaS gateway. Don't ship a web service
backed by your personal OAuth.

**macOS-first**: Claude's Desktop app bundle is auto-discovered on macOS. On
Linux/Windows the `claude` binary needs to be on `$PATH`. REPL's arrow-key
history needs `readline` (stdlib on macOS/Linux; Windows users may need
`pyreadline3`).

**Gemini (`agy`) specifics**: `agy` headless mode prints plain text (no JSON
event stream), so the wrapper can't surface per-token usage — `tokens in/out`
shows as `None`. Session resume uses `--conversation <UUID>` / `--continue`;
the conversation id is recovered from the newest `.db` in
`~/.gemini/antigravity-cli/conversations/`. Because `agy` runs full agentic
loops (web/shell/file), a turn can take longer than a one-shot completion, so
this provider defaults to a larger timeout (300s).

**No persistent usage tracking**: `UsageTracker` keeps per-provider aggregates
and recent-call history in process memory only. Restart = counters reset. For
long-term usage analytics you'd need to log separately.

## Comparison with similar projects

| Project | Language | CLI + Python import | 3-CLI subprocess | OpenAI server | Dashboard | REPL |
|---|---|---|---|---|---|---|
| **unified-cli** (this) | Python | ✅ | ✅ (direct) | ✅ | ✅ | ✅ |
| [oauth-cli-coder](https://github.com/codeninja/oauth-cli-coder) | Python | ✅ | ✅ (via tmux) | ❌ | ❌ | — |
| [coding-cli-runtime](https://pypi.org/project/coding-cli-runtime/) | Python | library only | ✅ | ❌ | ❌ | ❌ |
| [router-for-me/CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) | Go | ❌ (server only) | ✅ | ✅ | ✅ | ❌ |
| [codeking-ai/cligate](https://github.com/codeking-ai/cligate) | TypeScript | ❌ (server only) | ✅ | ✅ | — | ❌ |
| [PleasePrompto/ductor](https://github.com/PleasePrompto/ductor) | Python | ❌ (bot only) | ✅ | ❌ | ❌ | ❌ |
| [simonw/llm + llm-claude-code](https://github.com/simonw/llm) | Python | ✅ | Claude only | ❌ | ❌ | ❌ |
| [litellm](https://github.com/BerriAI/litellm) | Python | ❌ | direct API | ✅ | ❌ | ❌ |

**Closest neighbour**: `oauth-cli-coder` — same dual-mode idea, but uses `tmux`
sessions as the integration primitive (requires tmux on user's machine). This
project uses direct `subprocess.Popen` for a simpler deployment story
(stdlib-only core, no external process manager), adds the OpenAI-compatible
server + live dashboard + rich REPL + state-file sharing between CLI and
Python code.

**Closest library-only alternative**: `coding-cli-runtime` on PyPI — pure
Python library that wraps multiple coding CLIs per its PyPI page (verify the
exact set yourself). No CLI entry point, no server, no REPL.

If your use case is *just* "spawn a CLI and get text back" — `coding-cli-runtime`
is smaller. If you want dual-mode + richer infrastructure (state, server,
dashboard, REPL), this is the one.

## Project structure

```
unified_cli/
├── src/unified_cli/
│   ├── core.py          # Message, Response, Usage, ModelInfo dataclasses
│   ├── errors.py        # UnifiedError + classify() per-provider matchers
│   ├── discovery.py     # find_{claude,codex,gemini}_bin()
│   ├── base.py          # BaseProvider ABC + side-effect-aware retry
│   ├── providers/       # claude.py, codex.py, gemini.py
│   ├── conversation.py  # UnifiedConversation (cross-provider context)
│   ├── state.py         # ~/.unified-cli/state.json read/write
│   ├── usage.py         # UsageTracker (per-process aggregates)
│   ├── factory.py       # create() + route()
│   ├── cli.py           # doctor / setup / status / chat / repl / models
│   ├── repl.py          # interactive REPL with slash commands
│   ├── server.py        # FastAPI OpenAI-compat server + /dashboard
│   └── ui.py            # rich helpers (tables, panels)
├── tests/               # pytest offline/unit and server-hardening suite
└── examples/            # 8 runnable scripts
```

## License

MIT License · Copyright (c) 2026 Minwoo Kim — see [LICENSE](LICENSE).

Anyone is free to use, modify, and redistribute this software, provided the
copyright notice and license text are preserved in the redistribution.
Personal use of provider subscriptions (Claude Pro/Max, ChatGPT Plus/Pro,
Google AI Pro) is your own responsibility under each provider's Terms of
Service — see "Known limitations" above.

## Contributing

Issues and PRs welcome. Please run `pytest -q` before opening a PR — the full
offline suite should stay green.
