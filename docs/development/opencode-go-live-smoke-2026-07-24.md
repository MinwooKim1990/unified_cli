# OpenCode Go live smoke — 2026-07-24

This check used the user's already authenticated OpenCode Go account. No
credential, prompt body, account identifier, or auth-file content was recorded.

## Test target

- macOS arm64
- OpenCode CLI `1.18.0`
- official Homebrew executable:
  `/opt/homebrew/bin/opencode -> ../Cellar/opencode/1.18.0/bin/opencode`
- unified-cli source version `0.5.3`
- synthetic git workspace and synthetic 64×64 PNG

## OpenCode CLI result

The vendor CLI and OpenCode Go subscription worked:

| Check | Result |
|---|---|
| `opencode auth list` | OpenCode Go API credential detected |
| `opencode models --refresh` | 16 current `opencode-go/*` models returned |
| JSON chat with `opencode-go/deepseek-v4-flash` | Passed |
| Local `glob` + `read` tool call | Passed |
| `websearch` with `OPENCODE_ENABLE_EXA=true` | Passed |
| Synthetic PNG with `opencode-go/grok-4.5` | Passed |
| Synthetic PNG with `opencode-go/qwen3.7-plus` | Timed out after more than 140 seconds |

The refreshed Go catalog contained:

`deepseek-v4-flash`, `deepseek-v4-pro`, `glm-5.1`, `glm-5.2`, `grok-4.5`,
`hy3`, `kimi-k2.6`, `kimi-k2.7-code`, `kimi-k3`, `mimo-v2.5`,
`mimo-v2.5-pro`, `minimax-m2.7`, `minimax-m3`, `qwen3.6-plus`,
`qwen3.7-max`, and `qwen3.7-plus`.

## unified-cli result

OpenCode remains **Preview** because the current adapter did not complete a
call through any public unified-cli surface:

| Surface | Result |
|---|---|
| Python `configure_extension_provider("opencode")` | Failed while binding the launch context |
| Python `create("opencode", ...)` | Same configuration failure |
| Python `list_models("opencode", force_refresh=True)` | Same configuration failure |
| REPL | Provider is selectable, but `/model` only shows `default`; chat fails with the same configuration error |
| Browser Providers | OpenCode is listed; Verify returns HTTP 502 |
| Browser Models | Refresh returns HTTP 502 |
| Browser Chat | OpenCode is disabled by its current server policy |

The underlying diagnostic is `installation directory permissions are unsafe`.
The provenance check rejects the official Homebrew Cellar directory chain even
though the OpenCode executable and authentication work when invoked directly.
This test did not weaken that check or add an unsafe bypass.

The current adapter is also chat-only: it invokes a plain
`opencode run -- <prompt>` command with the literal model default `default`.
It does not yet forward a selected `provider/model`, discover the live model
catalog, attach images, opt into web search, or normalize OpenCode tool,
session, and usage events.

## Release decision

- Keep `opencode` at **Preview**.
- Keep the standalone `grok` provider at **Preview**. Passing the
  `opencode-go/grok-4.5` model checks OpenCode Go, not the separate Grok Build
  CLI adapter.
- Until the adapter is updated, use the official `opencode` CLI directly for
  this authenticated installation.

OpenCode can be reconsidered for Stable after the official Homebrew
installation passes provenance binding without relaxing unsafe-path checks,
live model selection is forwarded, Python/REPL/browser calls pass, and the
advertised image, web, tool, session, and usage behavior is covered.
