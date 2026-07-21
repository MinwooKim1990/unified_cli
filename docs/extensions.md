# Extensions

`unified-cli-ext` is separate from Core (`unified-cli`). Core continues to
support Claude, Codex, and Gemini (`agy`) with its existing defaults. Installing
Ext does not change those defaults, does not add an extension to Core's local
server allowlist, and does not install or configure any vendor software.

Stages 5B–5D install catalog metadata for exactly 11 inert Held providers. Each
is discoverable through an explicit entry point, has status **Held**, and is
incapable of starting a provider or external command. The adapter catalog
records `chat` as a provisional design target, while the Core plugin advertises
no executable capability. There is no supported provider chat command for
these entries yet.

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
An explicit request loads only that provider's entry point; Core then reports
support `held` and stops before any provider callback.

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

Only **Held** applies to every catalog entry below.

## Generated provider support

The machine-status table below is generated from the explicit Ext entry-point
plugins. The detailed candidate-transport catalog that follows remains a
manual design record.

<!-- BEGIN GENERATED EXT PROVIDER SUPPORT -->
| Provider ID | Support status | Core capabilities | Server |
|---|---|---|---|
| `cline` | `held` | `none` | `disabled` |
| `codebuddy` | `held` | `none` | `disabled` |
| `copilot` | `held` | `none` | `disabled` |
| `cursor` | `held` | `none` | `disabled` |
| `grok` | `held` | `none` | `disabled` |
| `kilo` | `held` | `none` | `disabled` |
| `kimi` | `held` | `none` | `disabled` |
| `mistral-vibe` | `held` | `none` | `disabled` |
| `opencode` | `held` | `none` | `disabled` |
| `qoder` | `held` | `none` | `disabled` |
| `qwen` | `held` | `none` | `disabled` |
<!-- END GENERATED EXT PROVIDER SUPPORT -->

## Stage 5B–5D catalog

“Candidate transport” records a provisional design direction, not a command
contract. “Auto-update containment” describes the intended boundary if an
adapter is later enabled; no Held metadata executes it today.

| Provider ID | Official binary/package | Candidate transport | Provisional adapter target | Status | Auto-update containment | Official documentation |
|---|---|---|---|---|---|---|
| `grok` | Grok CLI (`grok`) | JSONL | `chat` candidate; Core capability none | Held | Candidate `--no-auto-update`; requires verification before use | [Overview](https://docs.x.ai/build/overview) · [CLI reference](https://docs.x.ai/build/cli/reference) · [Headless scripting](https://docs.x.ai/build/cli/headless-scripting) |
| `kimi` | Kimi Code CLI (`kimi`), the current successor—not the legacy Python `kimi-cli` | JSONL | `chat` candidate; Core capability none | Held | Candidate opt-in `KIMI_CODE_NO_AUTO_UPDATE`; requires verification before use | [Getting started](https://moonshotai.github.io/kimi-code/en/guides/getting-started.html) · [Kimi command](https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html) · [Kimi ACP](https://moonshotai.github.io/kimi-code/en/reference/kimi-acp.html) |
| `copilot` | GitHub Copilot CLI (`copilot`, `@github/copilot`) | Plain text | `chat` candidate; Core capability none | Held | Candidate `--no-auto-update`; requires verification before use | [Install](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli) · [CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference) · [ACP server](https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server) |
| `cursor` | Cursor Agent CLI (`cursor-agent`) | JSON | `chat` candidate; Core capability none | Held | No containment claim yet; update behavior must be verified before use | [Install](https://cursor.com/docs/cli/installation) · [Parameters](https://cursor.com/docs/cli/reference/parameters) · [Output format](https://cursor.com/docs/cli/reference/output-format) · [ACP](https://cursor.com/docs/cli/acp) |
| `codebuddy` | CodeBuddy Code (`codebuddy`, `@tencent-ai/codebuddy-code`) | JSONL protocol candidate | `chat` candidate; Core capability none | Held | Candidate `DISABLE_AUTOUPDATER=1`; exact frame and config isolation require verification | [CLI reference](https://www.codebuddy.ai/docs/cli/cli-reference) · [Headless mode](https://www.codebuddy.ai/docs/cli/headless) · [ACP](https://www.codebuddy.ai/docs/cli/acp) |
| `qoder` | Qoder CLI (`qodercli`, `@qoder-ai/qodercli`) | ACP stdio candidate | `chat` candidate; Core capability none | Held | Candidate private setting `general.enableAutoUpdate=false`; ACP lifecycle requires verification | [Quick start](https://docs.qoder.com/en/cli/quick-start) · [ACP](https://docs.qoder.com/en/cli/acp) · [Permissions](https://docs.qoder.com/en/cli/permissions) |
| `mistral-vibe` | Mistral Vibe (`vibe`, `mistral-vibe`) | JSONL message stream candidate | `chat` candidate; Core capability none | Held | Candidate private config with update checks off; direct and `vibe-acp` paths require separate verification | [Install](https://docs.mistral.ai/getting-started/quickstarts/vibe-code/install-cli) · [CLI workflow](https://docs.mistral.ai/vibe/code/cli/work-with-cli) · [ACP surfaces](https://docs.mistral.ai/vibe/code/choose-cli-vscode-web-sessions) |
| `qwen` | Qwen Code (`qwen`, `@qwen-code/qwen-code`) | JSONL candidate | `chat` candidate; Core capability none | Held | Backend selection, credentials, update behavior, and event schema require verification | [Repository](https://github.com/QwenLM/qwen-code) · [Headless mode](https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/) · [Authentication](https://qwenlm.github.io/qwen-code-docs/en/users/configuration/auth/) |
| `cline` | Cline CLI (`cline`) | JSONL candidate; separate ACP candidate | `chat` candidate; Core capability none | Held | Candidate `CLINE_NO_AUTO_UPDATE=1`; stdin EOF, event schema, and local configuration isolation require verification | [CLI overview](https://docs.cline.bot/usage/cli-overview) · [CLI reference](https://docs.cline.bot/cli/cli-reference) · [Release source](https://github.com/cline/cline/tree/cli-v3.0.46/apps/cli) |
| `opencode` | OpenCode (`opencode`, package `opencode-ai`) | `JSONL one-shot` candidate | `chat` candidate; Core capability none | Held | Candidate disable controls require verification; stdin EOF, config/MCP isolation, and process/session lifecycle remain Stage 6 gates | [Documentation](https://opencode.ai/docs/) · [CLI](https://opencode.ai/docs/cli/) · [Server](https://opencode.ai/docs/server/) |
| `kilo` | Kilo Code (`kilo`, package `@kilocode/cli`) | `ACP stdio with an internal loopback server` candidate | `chat` candidate; Core capability none | Held | No verified auto-update containment claim yet; ACP loopback/process/config/permission lifecycles require Stage 6 verification | [CLI](https://kilo.ai/docs/code-with-ai/platforms/cli) · [CLI reference](https://kilo.ai/docs/code-with-ai/platforms/cli-reference) · [Release](https://github.com/Kilo-Org/kilocode/releases/tag/v7.4.11) |

The optional `acp` and `mcp` extras install protocol SDK dependencies only.
They do not activate a Held provider or make a provider call.

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
