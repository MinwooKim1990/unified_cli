# Extensions

`unified-cli-ext` is separate from Core (`unified-cli`). Core continues to
support Claude, Codex, and Gemini (`agy`) with its existing defaults. Installing
Ext does not change those defaults, does not add an extension to Core's local
server allowlist, and does not install or configure any vendor software.

Ext installs 18 explicit provider entry points. Grok Build is a read-tool-limited
**Preview** backed by offline fixtures. The other 17 entries are **Held** and
stop before provider construction, binary lookup, or command execution. No Ext
provider is enabled in Core server mode.

Vendor binaries, accounts, subscriptions, and their updates remain
user-owned. Installing Ext alone does not install a vendor CLI, log in, call a
service, or incur charges. Ext is not affiliated with the vendors listed here.

## Install and inspect

```bash
python -m pip install unified-cli-ext
python -c "import importlib.metadata as m; print([e.name for e in m.distribution('unified-cli-ext').entry_points if e.group == 'unified_cli.providers.v1'])"
```

The second command only displays installed entry-point names. It does not
verify a vendor installation, authentication state, or service availability.

Core keeps this discovery import-free. `unified-cli providers --include-ext`
therefore reports a new entry as lifecycle `discovered` and support `unknown`.
An explicit request loads only that provider's entry point. Held providers stop
before any provider callback. Grok continues only after its explicitly selected
local binary passes bounded version and feature probes.

## Grok Preview setup and boundary

Grok Build Preview is the sole runnable Ext provider. Its primary official
native installer is `https://x.ai/cli/install.sh`; the official npm package
`@xai-official/grok` is a vendor alternative, but this 0.1 Preview setup uses
the native install layout only. The known unrelated
`@vibe-kit/grok-cli` CLI shape is rejected. The minimum supported version is
`0.2.110`. Run the vendor installer in your normal user home, then copy its
resolved platform binary into the private canonical path expected by Ext:

```bash
curl -fsSL https://x.ai/cli/install.sh | bash
python - <<'PY'
import os
import shutil
import stat
from pathlib import Path

source = (Path.home() / ".grok" / "bin" / "grok").resolve(strict=True)
downloads = (Path.home() / ".grok" / "downloads").resolve(strict=True)
metadata = source.stat()
if (
    source.parent != downloads
    or not source.name.startswith("grok-")
    or not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid != os.getuid()
    or metadata.st_mode & 0o022
):
    raise SystemExit("refusing an unexpected Grok installer target")

root = Path.home() / ".unified-cli" / "providers" / "grok"
binary_dir = root / "bin"
provider_home = root / "home"
for directory in (root, binary_dir, provider_home):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
target = binary_dir / "grok"
temporary = binary_dir / ".grok.new"
temporary.unlink(missing_ok=True)
try:
    with source.open("rb") as incoming, temporary.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    temporary.chmod(0o500)
    os.replace(temporary, target)
finally:
    temporary.unlink(missing_ok=True)
print(target)
PY
```

Do not reuse a generic host login. Authenticate only that snapshot in its
isolated home, then register the same canonical binary and home through Core's
public extension-configuration API:

```bash
GROK_ROOT="$HOME/.unified-cli/providers/grok"
GROK_MANAGED_MCPS_ENABLED=false \
GROK_MANAGED_MCP_GATEWAY_TOOLS_ENABLED=false \
HOME="$GROK_ROOT/home" "$GROK_ROOT/bin/grok" login --device-auth
python - <<'PY'
from pathlib import Path
from unified_cli import ExtensionLaunchOverridesV1, configure_extension_provider

root = Path.home() / ".unified-cli" / "providers" / "grok"
configure_extension_provider(
    "grok",
    ExtensionLaunchOverridesV1(
        bin_path=str(root / "bin" / "grok"),
        provider_home=str(root / "home"),
    ),
)
PY
unified-cli chat "explain this project" --provider grok --model grok-build
```

These are copy-only instructions; Ext never runs installation or login itself.
Repeat the snapshot and registration after an intentional vendor CLI update.

For each prompt, the adapter fixes `--no-auto-update`, strict sandboxing,
`dontAsk`, and permits only `read_file`, `grep`, and `list_dir`. Plan,
subagents, memory, web search, and managed MCP initialization are off; the two
managed-MCP environment controls are fixed to `false` and caller values cannot
override them. Immediately before every turn it fails closed if it finds
`.grok`, `.envrc`, `.mcp.json`, `.cursor/mcp.json`,
`.cursor/hooks.json`, or `.claude` from the cwd up to the Git root (or at the
cwd only when it is outside a Git repository);
provider-home runtime config, plugins, hook directories or hook-path files; or
provider-home shell startup files, runtime config, plugins, hook directories or
hook-path files; or managed `/etc/grok` configuration. An otherwise empty,
auth-only isolated provider `HOME` is allowed.

Offline fixtures verify the adapter, but the real authenticated CLI smoke is
pending. Grok is therefore Preview, not Stable, and remains disabled in server
mode.

## Local installation receipts

Ext can record and later recheck the identity and metadata of an explicitly
selected local executable or npm launcher. A receipt describes local files; it
does not prove who published them or replace verification of the vendor's
official distribution channel. Capture and verification should happen as
close as practical to launch because another process with the same filesystem
access can change a path between those operations.

## Status vocabulary

| Status | Meaning |
|---|---|
| Stable | A released, supported integration with the documented compatibility evidence. |
| Preview | An enabled integration still being evaluated; its limits are documented. |
| Experimental | An enabled, limited-scope integration whose behavior may change. |
| Held | Discoverable metadata only. It is blocked before provider construction, binary lookup, or command execution. |

Grok is **Preview**; every other catalog entry below is **Held**.

## Generated provider support

The machine-status table below is generated from the explicit Ext entry-point
plugins. The detailed candidate-transport catalog that follows remains a
manual design record.

<!-- BEGIN GENERATED EXT PROVIDER SUPPORT -->
| Provider ID | Support status | Core capabilities | Server |
|---|---|---|---|
| `amp` | `held` | `none` | `disabled` |
| `cline` | `held` | `none` | `disabled` |
| `codebuddy` | `held` | `none` | `disabled` |
| `copilot` | `held` | `none` | `disabled` |
| `cursor` | `held` | `none` | `disabled` |
| `droid` | `held` | `none` | `disabled` |
| `gitlab-duo` | `held` | `none` | `disabled` |
| `grok` | `preview` | `chat, sessions, stream` | `disabled` |
| `hermes` | `held` | `none` | `disabled` |
| `kilo` | `held` | `none` | `disabled` |
| `kimi` | `held` | `none` | `disabled` |
| `mistral-vibe` | `held` | `none` | `disabled` |
| `oh-my-pi` | `held` | `none` | `disabled` |
| `opencode` | `held` | `none` | `disabled` |
| `pi` | `held` | `none` | `disabled` |
| `poolside` | `held` | `none` | `disabled` |
| `qoder` | `held` | `none` | `disabled` |
| `qwen` | `held` | `none` | `disabled` |
<!-- END GENERATED EXT PROVIDER SUPPORT -->

## Stage 5B–5F catalog

“Candidate transport” records a provisional design direction, not a command
contract. “Auto-update containment” describes the intended boundary if an
adapter is later enabled; no Held metadata executes it today.

The Grok, Kimi, Copilot, and Cursor rows record current official-documentation
research and pinned compatibility targets. Prompts are argv values and must not
be logged. Grok has an offline-fixture-verified one-shot bridge, but its real
authenticated CLI smoke is pending. Kimi, Copilot, and Cursor remain Held and
their factories refuse before binary resolution, environment reads, or
execution. ACP candidates are not enabled bridges.

| Provider ID | Official binary/package | Candidate transport | Provisional adapter target | Status | Auto-update containment | Official documentation |
|---|---|---|---|---|---|---|
| `grok` | xAI Grok Build (`grok`): primary native installer `https://x.ai/cli/install.sh`; official npm `@xai-official/grok` is alternative; rejects unrelated `@vibe-kit/grok-cli` CLI shape | Streaming JSONL | Explicit `-p` one-shot with `chat`, `stream`, `sessions`; minimum `0.2.110` | Preview | Fixed no-auto-update, strict sandbox, `dontAsk`, web/plan/subagent/memory/managed-MCP off, and only `read_file`, `grep`, `list_dir`; fail-closed workspace/home/system configuration preflight; offline fixtures pass, authenticated CLI smoke pending | [Repository](https://github.com/xai-org/grok-build) · [Overview](https://docs.x.ai/build/overview) · [CLI reference](https://docs.x.ai/build/cli/reference) · [Headless scripting](https://docs.x.ai/build/cli/headless-scripting) |
| `kimi` | Kimi Code CLI (`kimi`, `@moonshot-ai/kimi-code`), not legacy Python `kimi-cli` | Stream JSON candidate | `-p` one-shot auto-approves normal tools; Core capability none | Held | Candidate `KIMI_CODE_NO_AUTO_UPDATE=1` and `KIMI_DISABLE_TELEMETRY=1`; no per-run read-only/no-tools/web-off/MCP-off contract | [Getting started](https://moonshotai.github.io/kimi-code/en/guides/getting-started.html) · [Kimi command](https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html) · [Kimi ACP](https://moonshotai.github.io/kimi-code/en/reference/kimi-acp.html) |
| `copilot` | GitHub Copilot CLI (`copilot`, `@github/copilot`) | Plain-text one-shot candidate | Explicit read-only tool candidate; Core capability none | Held | Candidate `--no-auto-update` plus tool/MCP controls; JSONL schema and complete user/workspace MCP and dedicated-home isolation remain unverified | [Install](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli) · [CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference) · [ACP server](https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server) |
| `cursor` | Cursor Agent CLI (`agent` primary; `cursor-agent` legacy alias since 2026-01-08) | Final/stream JSON schema candidates | Positional-prompt ABI is not safely representable; Core capability none | Held | No verified read-only, MCP, or update containment; `CURSOR_API_KEY` is env-only and never argv | [Install](https://cursor.com/docs/cli/installation) · [Parameters](https://cursor.com/docs/cli/reference/parameters) · [Output format](https://cursor.com/docs/cli/reference/output-format) · [ACP](https://cursor.com/docs/cli/acp) |
| `codebuddy` | CodeBuddy Code (`codebuddy`, `@tencent-ai/codebuddy-code`) | JSONL protocol candidate | `chat` candidate; Core capability none | Held | Candidate `DISABLE_AUTOUPDATER=1`; exact frame and config isolation require verification | [CLI reference](https://www.codebuddy.ai/docs/cli/cli-reference) · [Headless mode](https://www.codebuddy.ai/docs/cli/headless) · [ACP](https://www.codebuddy.ai/docs/cli/acp) |
| `qoder` | Qoder CLI (`qodercli`, `@qoder-ai/qodercli`) | ACP stdio candidate | `chat` candidate; Core capability none | Held | Candidate private setting `general.enableAutoUpdate=false`; ACP lifecycle requires verification | [Quick start](https://docs.qoder.com/en/cli/quick-start) · [ACP](https://docs.qoder.com/en/cli/acp) · [Permissions](https://docs.qoder.com/en/cli/permissions) |
| `mistral-vibe` | Mistral Vibe (`vibe`, `mistral-vibe`) | JSONL message stream candidate | `chat` candidate; Core capability none | Held | Candidate private config with update checks off; direct and `vibe-acp` paths require separate verification | [Install](https://docs.mistral.ai/getting-started/quickstarts/vibe-code/install-cli) · [CLI workflow](https://docs.mistral.ai/vibe/code/cli/work-with-cli) · [ACP surfaces](https://docs.mistral.ai/vibe/code/choose-cli-vscode-web-sessions) |
| `qwen` | Qwen Code (`qwen`, `@qwen-code/qwen-code`) | JSONL candidate | `chat` candidate; Core capability none | Held | Backend selection, credentials, update behavior, and event schema require verification | [Repository](https://github.com/QwenLM/qwen-code) · [Headless mode](https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/) · [Authentication](https://qwenlm.github.io/qwen-code-docs/en/users/configuration/auth/) |
| `cline` | Cline CLI (`cline`) | JSONL candidate; separate ACP candidate | `chat` candidate; Core capability none | Held | Candidate `CLINE_NO_AUTO_UPDATE=1`; stdin EOF, event schema, and local configuration isolation require verification | [CLI overview](https://docs.cline.bot/usage/cli-overview) · [CLI reference](https://docs.cline.bot/cli/cli-reference) · [Release source](https://github.com/cline/cline/tree/cli-v3.0.46/apps/cli) |
| `opencode` | OpenCode (`opencode`, package `opencode-ai`) | `JSONL one-shot` candidate | `chat` candidate; Core capability none | Held | Candidate disable controls require verification; stdin EOF, config/MCP isolation, and process/session lifecycle remain Stage 6 gates | [Documentation](https://opencode.ai/docs/) · [CLI](https://opencode.ai/docs/cli/) · [Server](https://opencode.ai/docs/server/) |
| `kilo` | Kilo Code (`kilo`, package `@kilocode/cli`) | `ACP stdio with an internal loopback server` candidate | `chat` candidate; Core capability none | Held | No verified auto-update containment claim yet; ACP loopback/process/config/permission lifecycles require Stage 6 verification | [CLI](https://kilo.ai/docs/code-with-ai/platforms/cli) · [CLI reference](https://kilo.ai/docs/code-with-ai/platforms/cli-reference) · [Release](https://github.com/Kilo-Org/kilocode/releases/tag/v7.4.11) |
| `droid` | Factory Droid (`droid`, npm package `droid`) | Vendor stream JSON-RPC candidate | `chat` candidate; Core capability none | Held | Candidate update control, protocol envelope, permission flow, and process lifecycle require Stage 6 verification | [CLI reference](https://docs.factory.ai/reference/cli-reference) · [Droid Exec](https://docs.factory.ai/cli/droid-exec/overview) · [Package metadata](https://registry.npmjs.org/droid/latest) |
| `pi` | Pi Coding Agent (`pi`, package `@earendil-works/pi-coding-agent`) | Custom NDJSON RPC candidate | `chat` candidate; Core capability none | Held | Candidate `--offline` and resource-disable flags require Stage 6 verification; this is not JSON-RPC | [Package](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/package.json) · [README](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md) · [RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md) |
| `oh-my-pi` | Oh My Pi (`omp`, package `@oh-my-pi/pi-coding-agent`) | Custom NDJSON RPC candidate | `chat` candidate; Core capability none | Held | No verified update containment claim yet; configuration, resources, permissions, and process lifecycle require Stage 6 verification | [Repository](https://github.com/can1357/oh-my-pi) · [RPC](https://github.com/can1357/oh-my-pi/blob/main/docs/rpc.md) · [Approval mode](https://github.com/can1357/oh-my-pi/blob/main/docs/approval-mode.md) |
| `hermes` | Hermes Agent (`hermes`, PyPI `hermes-agent[acp]`) | ACP stdio candidate | `chat` candidate; Core capability none | Held | Hermes pins ACP 0.9.0 while Ext targets 0.11.x; compatibility, configuration, and lifecycle require Stage 6 verification | [PyPI](https://pypi.org/project/hermes-agent/) · [Repository](https://github.com/NousResearch/hermes-agent) · [ACP guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/acp.md) |
| `poolside` | Poolside Agent CLI (`pool`, official native release) | ACP stdio candidate | `chat` candidate; Core capability none | Held | No verified startup/update containment claim yet; proprietary binary identity, ACP schema, configuration, and removal require Stage 6 verification | [Install](https://docs.poolside.ai/cli/install) · [CLI reference](https://docs.poolside.ai/cli/cli-reference) · [Release](https://github.com/poolsideai/pool/releases/tag/v1.0.13) |
| `amp` | Amp CLI (`amp`, canonical package `@ampcode/cli`) | Claude-compatible streaming JSONL input/output candidate | `chat` candidate; Core capability none | Held | Tool approval is off by default; workspace settings, plugins, MCP, EOF/process lifecycle, and paid opt-in execution require isolated Stage 6 evidence | [Manual](https://ampcode.com/manual) · [Stream schema](https://ampcode.com/manual/appendix) · [Package](https://www.npmjs.com/package/@ampcode/cli) |
| `gitlab-duo` | GitLab Duo CLI (`duo`, compiled generic package or official npm package `@gitlab/duo-cli`) | One-shot JSON candidate | `chat` candidate; Core capability none | Held | Headless runs auto-approve tools; JSON schema 1.0, authentication/entitlement, context/MCP/hooks, cleanup, and isolation require Stage 6 evidence | [Overview](https://docs.gitlab.com/user/gitlab_duo_cli/) · [Usage](https://docs.gitlab.com/user/gitlab_duo_cli/use/) · [Setup](https://docs.gitlab.com/user/gitlab_duo_cli/set_up/) |

The optional `acp` and `mcp` extras install protocol SDK dependencies only.
They do not activate another provider or make a provider call.

## What promotion to an enabled integration requires

A future Stage 6 promotion is evaluated in an isolated environment for each
provider and version. Before a status can change, the project needs recorded,
repeatable evidence for:

- the exact vendor CLI install source and version;
- the observed authentication state and its user-visible behavior;
- prompt and output fixtures that establish the supported input/output form;
- cancellation and cleanup behavior, including what remains after an
  interrupted operation;
- permission behavior under the documented invocation; and
- session semantics, including how a session starts, continues, and ends.

This evidence is a compatibility gate, not a promise that a provider will be
promoted. Until it is complete and reviewed, the entry remains Held and cannot
run.

## Trust and ownership boundary

Extensions are installed Python code and run as trusted code in the host
Python process when loaded. Install only distributions you trust. Core owns
provider discovery and policy: extension providers must be explicitly
requested and are not selected by unprefixed model inference. The Core HTTP
server continues to reject extension providers at this stage.

For the Core extension ABI and trust boundary, see the
[provider plugin ABI](development/provider-plugin-abi-v1.md).
