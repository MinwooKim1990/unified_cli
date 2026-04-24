# Usage Guide

🇰🇷 [한국어 가이드](USAGE.ko.md) · 📘 [Back to README](README.md)

README is the overview; this file covers **day-to-day patterns and
troubleshooting** for both the CLI and the Python API.

## First-time setup

```bash
cd path/to/unified_cli     # after cloning

python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[server,dev]'

unified-cli setup          # interactive: installs missing CLIs + runs login flow
unified-cli doctor         # any time: health check
```

`doctor` should show 🟢 for all three providers. If one is 🟡, run
`unified-cli setup --provider <name>` to finish that specific login.

## The four daily usage patterns

| Goal | Tool |
|---|---|
| One-off question in terminal | `unified-cli chat "..."` |
| Continue the last terminal conversation | `unified-cli chat "..." --continue` |
| Free-form terminal dialogue | `unified-cli repl` |
| Integrate into your Python app | `from unified_cli import create` |

## Runnable examples

`examples/` contains 8 scripts you can run directly:

```bash
source .venv/bin/activate

python examples/01_hello.py             # greet each of the 3 providers
python examples/02_history.py           # multi-turn within one provider
python examples/03_multi_provider.py    # cross-provider conversation
python examples/04_streaming.py         # streaming event kinds
python examples/05_web_search.py        # built-in web search per provider
python examples/06_error_handling.py    # UnifiedError classification demo
python examples/07_openai_sdk.py        # use OpenAI SDK against local server
python examples/08_async.py             # achat / astream / asyncio.gather
```

## Quick terminal recipes

```bash
# single call
unified-cli chat "explain list reversal in one line" -m haiku

# continue the conversation you just started
unified-cli chat "what about in-place?" --continue

# resume a specific session (from a /save or dashboard)
unified-cli chat "pick up from earlier" --resume <session_id>

# force-start a fresh conversation (clears state file)
unified-cli chat "totally new topic" --new

# stream long responses
unified-cli chat "explain quicksort" -m haiku --stream

# fastest possible (Codex "spark" model)
unified-cli chat "quick q" -m gpt-5.3-codex-spark

# keep Claude concise
unified-cli chat "2+2?" --terse

# skip web-search to save tokens
unified-cli chat "hi" --no-web-search

# read prompt from stdin (long content)
cat error.log | unified-cli chat "diagnose this error" -m sonnet
```

## OpenAI-compatible server

Run the server:

```bash
source .venv/bin/activate
uvicorn unified_cli.server:app --port 8000
```

Point any OpenAI-compatible client at `http://localhost:8000/v1`:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"haiku","messages":[{"role":"user","content":"hi"}]}'
```

### Python (OpenAI SDK)
```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")
r = client.chat.completions.create(
    model="haiku",                     # auto-routes to Claude
    messages=[{"role": "user", "content": "hi"}],
    user="my-chat-1",                  # same value → conversation continues
)
print(r.choices[0].message.content)
```

### Model routing rules
- `claude/<m>`, `codex/<m>`, `gemini/<m>` — explicit prefix (highest priority)
- `claude-*`, `haiku`, `sonnet`, `opus` → Claude
- `gpt-*`, `o1-*`, `o3-*`, `codex-*` → Codex
- `gemini-*` → Gemini
- Anything else returns HTTP 400 `invalid_request_error`

### Cross-provider conversation
If you call with the same `user` value but a different `model`, the server
routes to the new provider and auto-injects the last 8 turns as prompt
prefix. Your OpenAI-compatible client thinks it's just talking to one model
the whole time.

## Python API cookbook

### Imports you'll typically need
```python
from unified_cli import (
    create, UnifiedConversation,            # main entry points
    Message, Response, Usage,               # data types
    UnifiedError, ErrorKind,                # error handling
    list_models, route,                     # utilities
    tracker,                                # in-memory usage tracker
)
```

### Pattern 1 — single call
```python
resp = create("claude").chat("hi")
print(resp.text, resp.session_id, resp.usage.output_tokens)
```

### Pattern 2 — external code manages history (typical chatbot)
```python
cli = create("codex")                       # create once, reuse
sessions: dict[str, str] = {}               # your app's user_id → session_id

def reply(user_id: str, prompt: str) -> str:
    r = cli.chat(prompt, session_id=sessions.get(user_id))
    sessions[user_id] = r.session_id        # store for next turn
    return r.text
```

### Pattern 3 — wrapper manages history + provider switching
```python
conv = UnifiedConversation()                # sticky=False by default
conv.send("My name is Minwoo.", provider="claude")
conv.send("What's my name?", provider="gemini")   # context auto-injected
for turn in conv.history():
    print(turn.provider, turn.prompt, "→", turn.text[:40])
```

### Pattern 4 — streaming with typed events
```python
for msg in create("claude").stream("latest Python release?"):
    if msg.kind == "text":
        print(msg.text, end="", flush=True)
    elif msg.kind == "tool_use":
        print(f"\n[tool: {msg.tool['name']}]")
    elif msg.kind == "usage":
        print(f"\n(tokens: {msg.usage.input_tokens}/{msg.usage.output_tokens})")
```
`Message.kind` values: `text` | `reasoning` | `tool_use` | `tool_result` |
`session` | `usage` | `done` | `error`.

### Pattern 5 — async in parallel
```python
import asyncio
from unified_cli import create

async def main():
    r = await asyncio.gather(
        create("claude", web_search=False).achat("A"),
        create("codex",  web_search=False).achat("B"),
        create("gemini", web_search=False).achat("C"),
    )
    for resp in r:
        print(resp.provider, resp.text.strip()[:30])

asyncio.run(main())
```

### Pattern 6 — error-driven provider fallback
```python
from unified_cli import create, UnifiedError

def robust_chat(prompt: str):
    for provider in ("claude", "codex", "gemini"):
        try:
            return create(provider).chat(prompt)
        except UnifiedError as e:
            if e.kind in ("auth_expired", "rate_limit", "model_not_allowed"):
                continue
            raise
    raise RuntimeError("all providers unavailable")
```

### Pattern 7 — share state between CLI and Python
```python
from unified_cli import create, load_last_session, save_last_session

# Python picks up wherever the CLI left off
state = load_last_session()                 # reads ~/.unified-cli/state.json
if state:
    resp = create(state.provider, model=state.model).chat(
        "follow-up from a Python script",
        session_id=state.session_id,
    )

# Or write from Python so the CLI's `--continue` picks up
r = create("claude").chat("starting from Python")
save_last_session(r.provider, r.model, r.session_id)
```

### Pattern 8 — provider-specific options
```python
from unified_cli import ClaudeProvider, CodexProvider, GeminiProvider

claude = ClaudeProvider(
    model="claude-sonnet-4-5",
    system_prompt="You are a terse code reviewer.",
    allowed_tools=["Read", "Grep"],
    disallowed_tools=["Bash", "Write"],
    permission_mode="bypassPermissions",
    cwd="/path/to/project",
    web_search=False,
    terse=True,
)

codex = CodexProvider(
    model="gpt-5.4",
    sandbox="workspace-write",                  # allow file edits
    full_auto=True,
    cwd="/path/to/project",
    config_overrides={"model_reasoning_effort": "high"},
)

gemini = GeminiProvider(
    model="gemini-3.1-flash",
    approval_mode="plan",                       # read-only
    cwd="/path/to/project",
)
```

## Provider-specific tips

### Claude
- Default model `claude-haiku-4-5`. Aliases `haiku` / `sonnet` / `opus` all work.
- For autonomous tool use set `permission_mode="bypassPermissions"` (wrapper
  does this automatically when `web_search=True`).
- If you want short answers to short questions pass `--terse` (CLI) or
  `terse=True` (ClaudeProvider).

### Codex
- Default model `gpt-5.4-mini`. ChatGPT subscription rejects `gpt-5`,
  `gpt-5.5`, `gpt-5-codex` — use one of `gpt-5.4-mini` / `gpt-5.4` /
  `gpt-5.2` / `gpt-5.3-codex-spark` instead.
- For file edits use `full_auto=True, cwd="..."` or set `sandbox="workspace-write"`.
- Web search is enabled via `-c tools.web_search=true` internally (wrapper
  handles this — just pass `web_search=True`).

### Gemini
- Default model `gemini-3.1-flash-lite-preview`. Note the `-preview` suffix —
  dropping it yields a 404.
- Session resume is index-based, so the wrapper does a `--list-sessions`
  lookup each turn to translate your UUID to an index (~500 ms overhead).
- `google_web_search` is structurally always available. `web_search=False` is
  approximated by `--approval-mode plan` (blocks tool use).
- `skip_trust=True` is the default so the wrapper works in any directory.

## Error handling

Every failure is a `UnifiedError` with a `kind` field:

| kind | Meaning | What to do |
|---|---|---|
| `auth_expired` | OAuth token expired | Re-run the provider's login, or set `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` (wrapper auto-retries once with the API key) |
| `rate_limit` | Weekly/daily quota hit | Switch providers or wait |
| `model_not_allowed` | Model rejected for your account | Check `unified-cli models` |
| `not_found` | Session/resource not found (e.g., wrong cwd for Gemini) | Use a fresh session |
| `network` | DNS/ECONNRESET | Already retried 2x — check connectivity |
| `config` | Bad provider name or routing | Error message + hint |
| `internal` | Unknown — check `.cause` field | Raw stderr first line |

Example:
```python
from unified_cli import UnifiedError, create

try:
    create("claude").chat("...")
except UnifiedError as e:
    if e.kind == "auth_expired":
        print("Run `claude /login` or set ANTHROPIC_API_KEY:", e.hint)
    elif e.kind == "rate_limit":
        create("codex").chat("...")             # try the next provider
    else:
        raise
```

## FAQ

**Q. Can I run many calls in parallel?**
→ Use `achat` / `astream` + `asyncio.gather`. See `examples/08_async.py`.

**Q. Web search makes calls expensive — how do I turn it off for short queries?**
→ `create(provider, web_search=False)` or `--no-web-search` on CLI.

**Q. The conversation got too long — will the context prefix overflow?**
→ Only the last 8 turns are injected on provider switch. Override with
`UnifiedConversation(context_window=16)` if you need more.

**Q. What do `x_session_id` / `x_provider` mean on the HTTP server response?**
→ Non-OpenAI extensions. They tell you which provider and session handled
the request — useful for debugging cross-provider routing.

**Q. Models list looks stale.**
→ Wrapper has a 1-hour in-process cache. Use
`list_models(provider, force_refresh=True)` or `unified-cli models --refresh`.

**Q. How do I deploy this headless (CI / server)?**
→ OAuth doesn't work headless. Set the API keys directly:
```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
export GEMINI_API_KEY=...
```

**Q. Can I fork / modify / redistribute?**
→ Yes — MIT license. Just keep the copyright notice from `LICENSE`.

## Architecture cheat sheet

```
factory.create(provider, ...)          ← simplest entry point
    └→ ClaudeProvider / CodexProvider / GeminiProvider
         └→ BaseProvider._run / _stream_run   ← subprocess + retry + api-key fallback
              └→ errors.classify              ← converts any failure to UnifiedError

UnifiedConversation                    ← multi-provider chat
    ├→ resolves (provider, model) per turn
    ├→ injects prior N turns as prefix when provider switches
    └→ reuses factory.create() internally

state.save/load_last_session           ← CLI <-> Python bridge
    └→ ~/.unified-cli/state.json

server.app (FastAPI)                   ← OpenAI-compat HTTP
    ├→ route(model) → (provider, model)
    ├→ stream=true → SSE
    └→ errors → {error:{type,...}} in OpenAI format
```
