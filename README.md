# unified-cli

**One Python + CLI interface for Claude Code, OpenAI Codex, and Google Gemini.**

🇰🇷 [한국어 README](README.ko.md) · 📖 [Detailed usage (KR)](USAGE.md)

Use all three AI coding CLIs — each signed in with your personal subscription
(Claude Pro/Max, ChatGPT Plus/Pro, Google AI Pro) — from a single unified
interface, both as a **terminal CLI** and as a **Python library you can
`import` in your own code**.

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
conv.send("Continue", provider="gemini")   # context auto-injected
```

## Why this exists

Each of the three CLIs (`claude`, `codex`, `gemini`) ships great subscription
auth but lives in its own world. Want to route "quick query" to the fastest
model regardless of provider? Want a single OpenAI-compatible `/v1/chat/completions`
endpoint backed by whatever CLI is cheapest/freshest? Want your Python app to
switch providers mid-conversation with automatic context handoff? That's what
this wrapper does — **as a CLI you can shell into, and as a Python package you
can import**.

## Features

- **Dual mode**: full-featured CLI (`unified-cli chat`, `repl`, `status`, ...)
  AND clean Python API (`from unified_cli import ...`) — same code, same state
- **Subscription-aware**: uses your existing `claude login` / `codex login` /
  `gemini` OAuth. Falls back automatically to `ANTHROPIC_API_KEY` /
  `OPENAI_API_KEY` / `GEMINI_API_KEY` if OAuth expires
- **Multi-turn history**: CLI via `--continue` / `--resume`, Python via
  `session_id=` or `UnifiedConversation`
- **Cross-provider conversation**: one `UnifiedConversation` can switch providers
  mid-chat; the last 8 turns auto-inject as context into the new provider's prompt
- **Unified streaming events**: `kind="text" | "tool_use" | "tool_result" |
  "reasoning" | "usage" | "session" | "done" | "error"` — normalized across
  the three native JSONL schemas
- **Web search by default**: Claude `WebSearch`, Codex `web_search`, Gemini
  `google_web_search` — all ON unless you pass `web_search=False`
- **Structured errors**: every failure → `UnifiedError(kind=...)` from one of
  seven categories (`auth_expired` / `rate_limit` / `model_not_allowed` /
  `not_found` / `network` / `config` / `internal`) with Korean recovery hints
- **OpenAI-compatible server**: drop-in `/v1/chat/completions` + auto-updating
  dashboard at `/dashboard`
- **Rich terminal UI**: `doctor` health table, `status --watch` live dashboard,
  `setup` interactive wizard, streaming spinner

## Default models (lightweight, subscription-friendly)

| Provider | Default | Why |
|---|---|---|
| Claude | `claude-haiku-4-5` | Fastest tier of Claude 4.x |
| Codex | `gpt-5.4-mini` | Fastest model allowed on ChatGPT subscription |
| Gemini | `gemini-3.1-flash-lite-preview` | Fastest Gemini 3.x variant |

Override via `-m <name>`. Use `-m gpt-5.3-codex-spark` for the absolute fastest
(tends to respond in ~2.5s vs Claude's 5-6s).

## Install

```bash
git clone https://github.com/MinwooKim1990/unified_cli.git
cd unified_cli
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[server,dev]'

unified-cli setup     # first-time onboarding (installs + logs into 3 CLIs)
```

Requires Python 3.9+ and at least one of `claude`, `codex`, `gemini` already
installed (or the setup wizard will install the missing ones for you).

## Usage at a glance

### CLI

```bash
# Single turn
unified-cli chat "explain python list reversal in one line"

# Continue the last conversation
unified-cli chat "what about in-place?" --continue

# Resume a specific session
unified-cli chat "continue from earlier" --resume <session_id>

# Interactive REPL with slash commands (/provider, /model, /history, /save, ...)
unified-cli repl

# Stream + web-search (both defaults)
unified-cli chat "latest Python release?" --stream

# Cheapest fast query
unified-cli chat "quick q" -m gpt-5.3-codex-spark

# Status & dashboard
unified-cli doctor          # one-time health check
unified-cli status --watch  # live terminal dashboard (5s refresh)
uvicorn unified_cli.server:app --port 8000  # + http://localhost:8000/dashboard
```

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
```

See [USAGE.md](USAGE.md) for the full cookbook (7 patterns: sync, async,
streaming, tool events, error fallback, CLI↔Python state sharing, advanced
provider options).

## Known limitations

**Speed**: every call spawns a fresh subprocess (`claude -p` / `codex exec` /
`gemini -p`) — these CLIs don't support a long-lived daemon. Measured latency:

| Stage | Claude | Codex | Gemini |
|---|---|---|---|
| Subprocess spawn | ~50 ms | ~60 ms | ~460 ms (Node bundle) |
| API round-trip (API round-trip) | 3–6 s | 2–3 s | 3–4 s |
| **Full chat turn** | **5–6 s** | **2.7–3 s** | **3–4 s** |

For the absolute fastest interactive feel, use `-m gpt-5.3-codex-spark`. Even
then, expect 2–3 seconds per turn. This is a **structural limit of the
subprocess architecture** — not something the wrapper can fix without either
(a) losing subscription auth by calling provider APIs directly, or (b) using
experimental daemon modes (`codex app-server`, `gemini --acp`) that aren't
fully stable yet.

**Subscription ToS**: each provider's terms forbid reselling/exposing your
personal subscription as a third-party service. This wrapper is designed for
**personal local automation**, not as a SaaS gateway. Don't ship a web service
backed by your personal OAuth.

**macOS-first**: Claude's Desktop app bundle is auto-discovered on macOS. On
Linux/Windows the `claude` binary needs to be on `$PATH`. REPL's arrow-key
history needs `readline` (stdlib on macOS/Linux; Windows users may need
`pyreadline3`).

**Gemini session resume** is index-based (CLI limitation). The wrapper does a
`--list-sessions` lookup each turn to translate UUID → index (~500 ms
overhead). Works, just slower than the other two.

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
Python library covering 4 CLIs (adds GitHub Copilot), but no CLI entry point,
no server, no REPL.

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
│   ├── base.py          # BaseProvider ABC + retry/fallback
│   ├── providers/       # claude.py, codex.py, gemini.py
│   ├── conversation.py  # UnifiedConversation (cross-provider context)
│   ├── state.py         # ~/.unified-cli/state.json read/write
│   ├── usage.py         # UsageTracker (per-process aggregates)
│   ├── factory.py       # create() + route()
│   ├── cli.py           # doctor / setup / status / chat / repl / models
│   ├── repl.py          # interactive REPL with slash commands
│   ├── server.py        # FastAPI OpenAI-compat server + /dashboard
│   └── ui.py            # rich helpers (tables, panels)
├── tests/               # 46 unit tests, stdlib only
└── examples/            # 8 runnable scripts
```

## License

MIT. Personal use of provider subscriptions is your responsibility under each
provider's ToS.

## Contributing

Issues and PRs welcome. Please run `python tests/test_errors.py` (and the
other `tests/test_*.py`) before opening a PR — all 46 should stay green.
