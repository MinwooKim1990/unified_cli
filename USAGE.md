# Usage Guide

🇰🇷 [한국어 가이드](USAGE.ko.md) · 📘 [Back to README](README.md)

README is the overview; this file covers **day-to-day patterns and
troubleshooting** for both the CLI and the Python API.

> ## ⚠️ ToS & account-ban risk
> You are responsible for complying with each provider's Terms of Service;
> automating these CLIs may breach them — **use at your own risk**. The intended
> safe pattern is **personal, local, individual use with your OWN subscription**
> (Anthropic officially supports headless `claude -p`). **Do not** expose the
> OpenAI-compatible server to others/over a network, route other people's
> requests through your subscription, share credentials, or resell/proxy access
> — those violate ToS and risk suspension/ban. Three safety defaults follow from
> this and are documented below:
> - The **`gemini` provider (Antigravity `agy`) is disabled by default** —
>   Google has banned individual accounts for automating it. Opt in with
>   `UNIFIED_CLI_ENABLE_GEMINI=1`.
> - The **`unified-cli serve` and `python -m unified_cli.server` launchers bind
>   to `127.0.0.1` by default** and refuse a non-loopback host unless
>   `UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1` is set. Raw `uvicorn` follows its own
>   host flag, but the app's ASGI guard returns HTTP 403 for a non-loopback bind,
>   peer, or Host until that opt-in. External mode also requires a non-whitespace
>   `UNIFIED_CLI_SERVER_AUTH_TOKEN` of at least 32 UTF-8 bytes and
>   `Authorization: Bearer …` on every request.
> - The server exposes a constrained **Claude-only** profile by default. Codex
>   and `agy` are rejected at the HTTP boundary unless an operator opts in from
>   an independently sandboxed container or VM.

## First-time setup

```bash
cd path/to/unified_cli     # after cloning

python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[server,dev]'

unified-cli setup          # interactive: installs missing CLIs + runs login flow
unified-cli doctor         # any time: health check
```

`doctor` reports each discovered provider's state. With the default ToS gate,
Gemini remains intentionally unavailable until `UNIFIED_CLI_ENABLE_GEMINI=1` is
set; for another unavailable provider, run `unified-cli setup --provider <name>`
to finish that specific setup.

**Running headless (launchd / cron / systemd / a server)?** The wrapped CLIs
assume an interactive TTY, so a background context can hit two traps: a minimal
`PATH` (binary "not found") and, on macOS, `claude` hanging on the login
Keychain (no TTY to unlock it). Fixes:

```bash
export CLAUDE_CLI_PATH=/opt/homebrew/bin/claude   # if PATH is minimal
claude setup-token                                # once, in a real terminal
export CLAUDE_CODE_OAUTH_TOKEN=<token>            # OAuth-equivalent, not metered
unified-cli doctor --headless   # run FROM the service context to prove auth works
```

By default the wrapper runs on your subscription OAuth and strips any inherited
`ANTHROPIC_API_KEY`/`OPENAI_API_KEY` from the child (so an exported key can't
silently switch you to per-token billing). See the README section “Running under
launchd / cron / a server” for the full recipe.

For an intentionally metered call, create a new Python provider/request and
pass the key explicitly; a failed OAuth turn is never replayed with it:

```python
from unified_cli import create

metered = create(
    "claude", extra_env={"ANTHROPIC_API_KEY": "<key-from-secret-store>"},
)
metered.chat("new request")
```

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
# Restores its provider/model and a still-valid saved working directory;
# an explicit --cwd always wins.
unified-cli chat "what about in-place?" --continue
unified-cli chat "use this checkout instead" --continue --cwd ~/work/project

# resume a specific session (from a /save or dashboard)
unified-cli chat "pick up from earlier" --resume <session_id>

# force-start a fresh conversation (clears state file)
unified-cli chat "totally new topic" --new

# choose the provider for model-unset new chats and REPL sessions
unified-cli config default-provider codex
unified-cli config default-provider --reset

# inspect the installed package without any provider discovery
unified-cli --version

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

# exact extension selection; the slash-containing model ID stays literal
unified-cli chat "hello" --provider exact-extension-id --model vendor/family/model

# image input (one or many; works for all 3 providers)
unified-cli chat "what's in this photo?" --image cat.png -m haiku
unified-cli chat "compare these two charts" --image a.png --image b.png -m gpt-5.4-mini
```

## Interactive REPL

```bash
unified-cli repl                              # configured default (Claude until changed)
unified-cli repl --provider codex -m gpt-5.4-mini
unified-cli repl --provider exact-extension-id -m vendor/family/model
unified-cli repl --no-web-search              # disable web search
unified-cli repl --lang ko                    # Korean UI
```

The REPL is powered by `prompt_toolkit` (a **core dependency**, so it works
straight from `pip install unified-cli` — there is no `[repl]` extra). In a
real terminal, type `/` to get a **live as-you-type dropdown** of every slash
command, so you don't have to memorize them. When stdin/stdout is not a TTY
(e.g. piped input), the REPL falls back to a plain `input()` loop exposing the
same commands.

Inside the REPL, slash commands let you change context without restarting:

| Command | What it does |
|---|---|
| `/help` | List all commands (localized to the current language) |
| `/model [literal\|--refresh]` | A literal sets the current provider's model without probing. Core's no-argument picker uses its in-memory cache/fallback; an extension uses only its descriptor default and last successful explicit refresh snapshot. |
| `/provider [exact-id]` | An exact ID loads only that extension's metadata. The no-argument picker shows Core plus extension descriptors already loaded in this process. |
| `/status` | Show a process-local state/session/usage/descriptor snapshot without provider probes. |
| `/lang <en\|ko>` | Switch the UI language live and persist it to `~/.unified-cli/settings.json` |
| `/new` | Reset the conversation (drop history) |
| `/save` | Show current `session_id` + how to resume from CLI |
| `/history [N]` | Show last N turns (default 10) |
| `/tokens` | Per-provider usage aggregate for this REPL session |
| `/doctor` | With Core selected, show only the existing Core health table. With an extension selected, call only that extension's explicit doctor; arbitrary return data is never rendered. |
| `/image <path>` | Attach an image for the next user message (repeatable) |
| `/images` | List currently pending image attachments |
| `/clear-images` | Drop pending attachments |
| `/exit`, `/quit`, Ctrl+D | Exit (the last session_id is saved → `chat --continue`) |

Command history persists to `~/.unified-cli/repl_history` between sessions
(the file is created with `0o600` permissions).

## Language (i18n)

The whole CLI/REPL is localized; English is the default and Korean is
available. The language is resolved in this order:

1. `--lang {en,ko}` global flag (e.g. `unified-cli --lang ko chat "안녕"`)
2. `~/.unified-cli/settings.json` (written by the REPL's `/lang` command)
3. `$UNIFIED_CLI_LANG` environment variable (`export UNIFIED_CLI_LANG=ko`)
4. Default: English

```bash
unified-cli --lang ko doctor          # one-off Korean output
export UNIFIED_CLI_LANG=ko            # whole shell session in Korean
# In the REPL:
[claude/haiku] > /lang ko             # switch live + persist
```

## Image input (multimodal)

All three providers can read images. The wrapper hides the per-provider
mechanism behind one common API:

```python
from unified_cli import create
create("claude").chat("describe", images=["cat.png"])
create("codex").chat("describe", images=["cat.png"])
create("gemini").chat("describe", images=["cat.png"])  # default gemini-3.5-flash
```

For direct Python and CLI calls, use trusted local data (mix freely in one
call):

```python
from pathlib import Path
from unified_cli import Attachment

images=[
    "cat.png",                                 # local file path (str)
    Path("/tmp/dog.jpg"),                      # pathlib.Path
    open("photo.webp","rb").read(),            # raw bytes
    Attachment(path="cat.png", media_type="image/png"),  # explicit
    Attachment(bytes_=raw_bytes, media_type="image/png"), # explicit bytes
]
```

How each provider handles it (handled automatically by the wrapper):

| Provider | Mechanism | Notes |
|---|---|---|
| **Codex** | `-i, --image <FILE>` flag | Native, repeatable. Requires `codex` CLI ≥ 0.129. With images, prompt is sent via stdin (CLI requirement). |
| **Claude** | Read tool | The wrapper permits `Read` for an attachment and prepends its path to the prompt so Claude Code can vision-process it. It does **not** automatically select `bypassPermissions`. |
| **Gemini (`agy`)** | `@<path>` prompt reference | The path is prepended to the prompt. Tool approvals remain enabled unless the caller explicitly chooses the risky `skip_permissions=True` option. |

`http(s)` URLs and `data:` URIs are normalized for validation but deliberately
raise `UnifiedError(kind="config")` in the subprocess providers. The wrapper
never fetches a remote image or turns an untrusted URL into a local-file read;
download or decode trusted data yourself and pass a path or bytes.

Per-provider supported formats / limits (subject to upstream changes):
- **Claude** — PNG, JPEG, GIF, WebP. ~100 images and 32 MB total per request.
- **Codex** — Whatever the underlying ChatGPT vision-capable models accept (typically PNG, JPEG, WebP).
- **Gemini** — PNG, JPEG, WEBP, HEIC, HEIF. Up to 3,600 images per request, ~20 MB inline.

CLI:
```bash
unified-cli chat "describe" --image foo.png --image bar.jpg -m gpt-5.4-mini
```

REPL:
```text
[claude/haiku] > /image photo.png
[claude/haiku] > /image second.jpg
[claude/haiku] > what's different about these two?
```

OpenAI-compatible server (multi-content schema):
```python
client.chat.completions.create(
    model="haiku",
    messages=[{"role":"user","content":[
        {"type":"text","text":"describe"},
        {"type":"image_url",
         "image_url":{"url":"data:image/png;base64,iVBOR..."}}
    ]}],
)
```

The HTTP boundary is intentionally stricter than the direct API:

- `image_url.url` must be one canonical base64 URI:
  `data:image/png;base64,...`, `data:image/jpeg;base64,...`,
  `data:image/gif;base64,...`, or `data:image/webp;base64,...`. Its decoded
  signature must match the declared MIME type.
- Remote URLs and filesystem paths are rejected rather than fetched or read.
- Default limits are 4 images per message, 4 MiB decoded per image, and a
  24 MiB request body. Operators may tune the explicit
  `UNIFIED_CLI_SERVER_MAX_IMAGES`, `UNIFIED_CLI_SERVER_MAX_IMAGE_BYTES`, and
  `UNIFIED_CLI_SERVER_MAX_BODY_BYTES` environment variables.

## OpenAI-compatible server

Run the server:

```bash
source .venv/bin/activate
# Uvicorn defaults to 127.0.0.1; an explicit external host is denied by the
# app's ASGI guard unless external mode is explicitly enabled.
uvicorn unified_cli.server:app --port 8000
# Dashboard:  http://localhost:8000/dashboard   (redesigned: stat cards, health
#             cards, latency/token sparklines, per-model usage bars)
#             http://localhost:8000/             redirects to /dashboard
```

> **Localhost-only by default.** `unified-cli serve` and
> `python -m unified_cli.server` bind `127.0.0.1` and **refuse a non-loopback
> host** (e.g. `0.0.0.0`) unless you explicitly set
> `UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1`. Raw `uvicorn ... --host 0.0.0.0` can
> still open a listener, but the app's ASGI guard returns HTTP 403 for that
> non-loopback bind, peer, or Host until the same opt-in is set. It logs a
> personal-use warning on startup. Exposing your personal subscription to other
> people / over a network violates the providers' ToS and **risks an account
> ban** — keep it local.

> **External mode is for one trusted client, not a public proxy.** It requires
> both `UNIFIED_CLI_ALLOW_EXTERNAL_BIND=1` and a non-whitespace
> `UNIFIED_CLI_SERVER_AUTH_TOKEN` of at least 32 UTF-8 bytes. All routes,
> including diagnostics, require `Authorization: Bearer <token>`. Put it behind
> TLS and do not treat this token as per-user authorization; the dashboard is
> designed for loopback use.

> **Provider isolation.** The default HTTP profile permits only Claude models:
> text requests run in Claude safe mode with no tools, and image requests get a
> scoped read permission for the supplied image bytes only. Codex and `agy`
> are rejected by default because their agentic CLI sandboxes do not provide
> confidential-data isolation for arbitrary HTTP requests. Set
> `UNIFIED_CLI_SERVER_ALLOW_AGENTIC_PROVIDERS=1` only inside an independently
> sandboxed container or VM with a deliberately scoped workspace mount. That
> opt-in is not authentication and does not make network exposure safe.

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

For an intentionally restricted external deployment, use the token as the SDK
API key and terminate TLS outside the package:

```python
import os
client = OpenAI(base_url="https://trusted.example/v1",
                api_key=os.environ["UNIFIED_CLI_SERVER_AUTH_TOKEN"])
```

### Model routing rules
- `claude/<m>`, `claude-*`, `haiku`, `sonnet`, `opus` → Claude (available by
  default)
- `codex/<m>`, `gpt-*`, `o1-*`, `o3-*`, `codex-*` → Codex (HTTP 403 by
  default)
- `gemini/<m>`, `gemini-*` → Gemini / `agy` (HTTP 403 by default)
- Anything else returns HTTP 400 `invalid_request_error`.

### Conversation continuity
The same `user` value keeps a bounded local conversation history (the last
eight turns by default) for subsequent Claude calls. Cross-provider HTTP
handoff is available only after the explicit, externally sandboxed agentic
provider opt-in above; direct Python `UnifiedConversation` remains the
recommended interface for local cross-provider work.

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

### Pattern 8 — image input (multimodal)
```python
from unified_cli import create

# All three providers accept the same `images=` argument
for provider, model in [("claude", "haiku"),
                         ("codex",  "gpt-5.4-mini"),
                         ("gemini", "gemini-3.5-flash")]:
    r = create(provider, model=model).chat(
        "what color is this image?",
        images=["/path/to/cat.png"],
    )
    print(provider, "→", r.text.strip())

# Mix multiple input forms in a single call
r = create("codex").chat(
    "compare these",
    images=[
        "left.png",
        b"\\x89PNG...raw bytes...",
    ],
)

# Streaming + image
for msg in create("gemini", model="gemini-3.5-flash").stream(
    "describe each", images=["a.png", "b.png"],
):
    if msg.kind == "text":
        print(msg.text, end="", flush=True)
```

See the **Image input (multimodal)** section above for per-provider
mechanism details and limits.

### Pattern 9 — provider-specific options
```python
from unified_cli import ClaudeProvider, CodexProvider, GeminiProvider

claude = ClaudeProvider(
    model="claude-sonnet-4-5",
    system_prompt="You are a terse code reviewer.",
    allowed_tools=["Read", "Grep"],
    disallowed_tools=["Bash", "Write"],
    permission_mode="dontAsk",                 # deny unapproved tool use
    cwd="/path/to/project",
    web_search=False,
    terse=True,
)

codex = CodexProvider(
    model="gpt-5.4",
    sandbox="workspace-write",                  # allow file edits
    full_auto=True,
    cwd="/path/to/project",
    config_overrides={
        "model_reasoning_effort": "high",       # string is TOML-quoted
        "tools.web_search": True,
        "limits.max_tokens": 512,
    },
)

gemini = GeminiProvider(
    model="gemini-3.1-flash",
    skip_permissions=False,                      # default: keep CLI approvals
    cwd="/path/to/project",
)
```

`CodexProvider.config_overrides` accepts dotted bare TOML keys and values of
type `str`, `bool`, `int`, finite `float`, or nested `list`/`tuple` values of
those types. Strings are quoted safely before `codex exec` receives them;
other value types raise `ValueError` instead of being passed ambiguously.

## Provider-specific tips

### Claude
- Default model `claude-haiku-4-5`. Aliases `haiku` / `sonnet` / `opus` all work.
- Choose a permission mode deliberately for unattended tool use. The wrapper
  never changes it solely because web search or images are enabled.
  `permission_mode="bypassPermissions"` grants broad authority and is for
  trusted local inputs only.
- If you want short answers to short questions pass `--terse` (CLI) or
  `terse=True` (ClaudeProvider).

### Codex
- Default model `gpt-5.4-mini`. ChatGPT subscription rejects `gpt-5`,
  `gpt-5.5`, `gpt-5-codex` — use one of `gpt-5.4-mini` / `gpt-5.4` /
  `gpt-5.2` / `gpt-5.3-codex-spark` instead.
- For file edits use `full_auto=True, cwd="..."` or set `sandbox="workspace-write"`.
- Web search is enabled via `-c tools.web_search=true` internally (wrapper
  handles this — just pass `web_search=True`).

### Gemini (now the Antigravity `agy` CLI)
- ⚠️ **Disabled by default.** Automating `agy` has gotten individual Google
  accounts **banned**, so the `gemini` provider only activates when
  `UNIFIED_CLI_ENABLE_GEMINI=1` is set in the environment. Without it, direct
  CLI/Python `gemini`/`agy` calls raise a config error. The HTTP server has an
  independent stricter policy and returns HTTP 403 for Gemini by default, even
  when this gate is set; it requires the separate agentic-provider opt-in inside
  an external sandbox. Enable direct use at your own risk:
  ```bash
  export UNIFIED_CLI_ENABLE_GEMINI=1
  ```
- The old `gemini` CLI is blocked for individual accounts
  (`IneligibleTierError → migrate to Antigravity`). The `gemini` provider now
  wraps `agy` (`~/.local/bin/agy`), discovered via `AGY_CLI_PATH` / PATH /
  `~/.local/bin/agy`.
- Default model `gemini-3.5-flash`. `agy --model` accepts both slugs
  (`gemini-3.5-flash`, `gemini-3.1-pro`) and the display names from
  `agy models` (`Gemini 3.5 Flash (Medium)`, `Claude Sonnet 4.6 (Thinking)`,
  `GPT-OSS 120B (Medium)`, ...). Unknown names silently fall back to default.
- Fully agentic: web search / shell / file tools run on the agent's own
  decision. Approvals are enabled by default. `skip_permissions=True` passes
  `--dangerously-skip-permissions` and is a risky, explicit opt-in for trusted
  local automation only.
- `web_search=` is effectively a no-op — `agy` always may search.
- Headless output is plain text, so there is **no token-usage reporting**
  (`usage` fields are `None`).
- Sessions: `--conversation <UUID>` / `--continue`; the id is read back from
  the newest `.db` in `~/.gemini/antigravity-cli/conversations/`.
- This provider defaults to a 300s timeout because agentic loops take longer.

## Error handling

Every failure is a `UnifiedError` with a `kind` field:

| kind | Meaning | What to do |
|---|---|---|
| `auth_expired` | OAuth token expired | Re-run the provider's login. Authentication failures are never replayed with a different credential. |
| `rate_limit` | Transient 429 or weekly/daily quota hit | A pre-turn transient 429 may retry within strict delay caps; quota exhaustion does not. Otherwise switch providers or wait. |
| `model_not_allowed` | Model rejected for your account | Check `unified-cli models` |
| `not_found` | Session/resource not found (e.g., wrong cwd for Gemini) | Use a fresh session |
| `network` | DNS/connection failure | Only clearly pre-turn transient failures retry automatically. Failures after output or possible tool execution do not. |
| `resource_limit` | A local output, stream, or HTTP safety ceiling was reached | Reduce the request/output; raise an explicit limit only for a trusted workload |
| `config` | Bad provider name or routing | Error message + hint |
| `internal` | Unknown — check `.cause` field | Raw stderr first line |

Example:
```python
from unified_cli import UnifiedError, create

try:
    create("claude").chat("...")
except UnifiedError as e:
    if e.kind == "auth_expired":
        print("Run `claude /login`:", e.hint)
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
→ Use the provider's supported headless auth first: for Claude, create a
`CLAUDE_CODE_OAUTH_TOKEN` with `claude setup-token`; Codex uses its own CLI
login state. `agy` requires an existing OAuth session. The wrapper never swaps
credentials to replay a failed turn. If you intentionally want metered API
billing, pass the provider key explicitly through `extra_env` for that provider
instance and issue a new request:
```bash
export CLAUDE_CODE_OAUTH_TOKEN=<token>
```

```python
from unified_cli import create

metered = create(
    "claude", extra_env={"ANTHROPIC_API_KEY": "<key-from-secret-store>"},
)
metered.chat("new request")
```

**Q. Can I fork / modify / redistribute?**
→ Yes — MIT license. Just keep the copyright notice from `LICENSE`.

## Architecture cheat sheet

```
factory.create(provider, ...)          ← simplest entry point
    └→ ClaudeProvider / CodexProvider / GeminiProvider
         └→ BaseProvider._run / _stream_run   ← subprocess + side-effect-aware retry
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
