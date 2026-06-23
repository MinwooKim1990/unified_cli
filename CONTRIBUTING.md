# Contributing to `unified-cli`

Thanks for your interest in improving `unified-cli`. This guide covers the
basics for getting set up and sending a change.

## Core principle (read this first)

`unified-cli` is a thin wrapper around the **official agentic CLIs** —
Claude Code (`claude`), OpenAI Codex (`codex`), and Google Antigravity
(`agy`). It drives them as subprocesses so users rely on their existing CLI
subscription auth through one Python API and an OpenAI-compatible server.

- **It ships no credentials and stores none.** Authentication is owned
  entirely by the underlying CLIs.
- **Never log, print, or transmit auth material** (tokens, API keys, cookies,
  session secrets, auth headers). Do not add features that capture or forward
  credentials.

Any change must preserve these invariants.

## Dev setup

Use a virtual environment and install the package editable with the dev and
server extras:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev,server]"
```

## Running tests

```bash
pytest
```

The test suite is **offline / unit-level** — it does not invoke the real
`claude` / `codex` / `agy` binaries and needs no provider credentials. CI runs
exactly this suite (plus a build + `twine check`), so keep tests offline; do
not add tests that require a live provider or network access.

## Repository layout

```
src/unified_cli/        # package source (src layout)
  cli.py                # CLI entry point (unified-cli = unified_cli.cli:main)
  conversation.py       # UnifiedConversation, cross-provider context
  base.py               # BaseProvider abstraction
  providers/            # one module per wrapped CLI
    claude.py           # ClaudeProvider  -> claude
    codex.py            # CodexProvider   -> codex
    gemini.py           # GeminiProvider  -> agy (Antigravity)
tests/                  # pytest suite (offline)
examples/               # usage examples
```

### A note on the `gemini` key

The third provider's internal name is still **`gemini`** (module
`providers/gemini.py`, class `GeminiProvider`, provider key `gemini`), but it
now wraps Google **Antigravity** (`agy`), not the standalone Gemini CLI. The
internal `gemini` identifier is kept for backward compatibility — keep using
it in code and tests; do not rename it as part of an unrelated change.

## Pull requests

- Keep the test suite passing (`pytest`); add tests for new behavior.
- Match the existing code style and structure — small, focused diffs.
- Update docs (`README.md` / `USAGE.md` and their `.ko` variants) when you
  change user-facing behavior.
- Add a `CHANGELOG.md` entry for notable changes.
- Never introduce credential handling, logging of secrets, or steps that
  require live provider calls in tests/CI.

See [RELEASING.md](RELEASING.md) for how releases are cut and published.
