# Grok 0.2.111 representative smoke

Verified on 2026-07-23 with official native Grok `0.2.111`, commit marker
`94172f2aa4e5`, on macOS arm64. The copied binary SHA-256 was
`e1fafdfffe14f339460befaf194360e8f90bfd02efe8a4f24cfa1c7aea657ffe`.

The run used an isolated device-code login and a private (`0600`) provider
config. Before the final adapter call, that config was updated to the exact safe
template documented by Ext. The only visible/default model was `grok-4.5`. No
credentials, prompts, responses, account identifiers, or session IDs are
recorded here.

## Covered behavior

- Direct chat, token streaming, and usage mapping completed through Ext.
- Streaming JSONL accepted `thought`, `text`, and `end`; `thought` was dropped.
  The `end` record contained `modelUsage`, `num_turns`, `requestId`, `sessionId`,
  `stopReason`, `type`, and `usage`. The raw observed `usage` object included
  `input_tokens`, `cache_read_input_tokens`, `output_tokens`,
  `reasoning_tokens`, and `total_tokens`. Ext maps only input, cache-read, and
  output into Core's usage fields; Core computes its normalized total as input
  plus output. This smoke does not claim that the raw reasoning counter is
  separately mapped.
- Session creation and resume used the same returned session ID.
- An invalid model returned a safe error. Cancellation completed in about 196 ms
  and left no extra process.
- A follow-up synthetic repository check enabled both fixed
  `GROK_RESPECT_GITIGNORE=1` and `[tools] respect_gitignore = true`. Grok
  reported the ignored canary file as blocked and did not return its synthetic
  contents.
- The public path `configure_extension_provider(...)` → `create("grok")` →
  `chat` → `clear` completed.

## Scope

This is evidence for one native version, platform, and authentication sample.
It does not make Grok Stable, enable Ext providers on public-compatible
`/v1/*` routes, establish every provider's behavior, or guarantee zero risk.
The loopback-only management UI may invoke Grok only when the user explicitly
selects it in a registered workspace.
