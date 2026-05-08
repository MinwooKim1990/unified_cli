# Phase 0 — Flagship Verification Matrix

Live verification of every flagship/listed model across the three providers,
to determine which model IDs actually work end-to-end. This drove the
`_HARDCODED` updates in Phase 1.

Run: 2026-05-08, MinwooKim1990 macOS, OAuth-authenticated CLIs (no API keys).

## Claude (`claude` CLI 2.1.111)

`claude -p --model <id> --output-format json "say:ok"` (cwd: /tmp)

| Model | Direct CLI | Resolved (`modelUsage` key) | Wrapper |
|---|---|---|---|
| `claude-opus-4-7` | ✅ | claude-opus-4-7 | ✅ |
| `claude-sonnet-4-6` | ✅ | claude-sonnet-4-6 | ✅ |
| `claude-haiku-4-5` | ✅ | claude-haiku-4-5 | ✅ |
| `opus` (alias) | ✅ | claude-opus-4-7 | ✅ |
| `sonnet` (alias) | ✅ | claude-sonnet-4-6 | ✅ |
| `haiku` (alias) | ✅ | claude-haiku-4-5-20251001 | ✅ |

**Findings:**
- All three current GA tiers work. Aliases `opus` / `sonnet` / `haiku` correctly
  map to the latest GA snapshot (4.7 / 4.6 / 4.5).
- The wrapper passes every ID through (no allowlist filtering).
- Wrapper currently exposes the *requested* model in the session panel, not
  the *resolved* model — which means `-m opus` users can't tell whether 4.7
  or 4.5 was actually used. Tracked as F6 in Phase 1.

## Codex (`codex` CLI 0.112.0)

`codex exec --json --skip-git-repo-check -m <slug> -s read-only "say:ok"`
(cwd: /tmp). User on ChatGPT subscription OAuth (`~/.codex/auth.json`).

| Model | Direct CLI | Note | Wrapper |
|---|---|---|---|
| `gpt-5.5` | ❌ | "The 'gpt-5.5' model requires a newer version of Codex. Please upgrade to the latest app or CLI and try again." | (skipped) |
| `gpt-5.4` | ✅ | Strong everyday model | ✅ |
| `gpt-5.4-mini` | ✅ | Default, fastest mainstream | ✅ |
| `gpt-5.2` | ✅ | Older flagship | ✅ |
| `gpt-5.3-codex` | ✅ | Coding-specialized | ✅ |
| `gpt-5.3-codex-spark` | ✅ | Lightweight, fastest | ✅ |
| `codex-auto-review` | ✅ | Review specialist | ✅ |

**Findings:**
- Six of seven cached slugs work end-to-end on ChatGPT subscription auth.
- `gpt-5.5` is **not** rejected for subscription tier — it's rejected because
  the user's `codex` CLI is at 0.112.0 and `gpt-5.5` was added in a newer
  version. Solution: `brew upgrade codex` or `npm i -g @openai/codex@latest`.
  After upgrade, `gpt-5.5` becomes available.
- Earlier audits referenced "gpt-5.5 not allowed on ChatGPT account" — that was
  a misread of an older error message. The current message is clear ("requires
  newer Codex").
- `gpt-5.3-codex` and `codex-auto-review` were not in the hardcoded list but
  do work — added in Phase 1.

## Gemini (`gemini` CLI 0.39.1)

**Could not run live verification — account quota exhausted at probe time
("You have exhausted your capacity on this model. Your quota will reset after
14h19m0s.")**

`gemini -p "say:ok" -m <id> --output-format json --skip-trust` returned
`TerminalQuotaError` (HTTP 429, `QUOTA_EXHAUSTED`) for every probed ID, so
this matrix is built from earlier audit data + official docs at
[ai.google.dev/gemini-api/docs/models](https://ai.google.dev/gemini-api/docs/models).

| Model | Verified earlier | Source |
|---|---|---|
| `gemini-3.1-pro-preview` | ⏳ pending re-test | docs (release 2026-02-19) |
| `gemini-3-flash-preview` | ⏳ pending re-test | docs (3.x flash, **note: not 3.1**) |
| `gemini-3.1-flash-lite-preview` | ✅ (default; previously verified) | docs |
| `gemini-3.1-flash-lite` | ⏳ pending re-test | docs (stable promotion) |
| `gemini-2.5-pro` | ⏳ pending re-test | docs (legacy, shutdown 2026-10-16) |
| `gemini-2.5-flash` | ⏳ pending re-test | docs (legacy) |
| `gemini-2.5-flash-lite` | ⏳ pending re-test | docs (legacy) |

**Bad IDs to remove** (previously in `_HARDCODED["gemini"]`):
- `gemini-3.1-pro` (no `-preview`) — does not exist (per agent docs research)
- `gemini-3.1-flash` (no `-preview`) — does not exist

The current default `gemini-3.1-flash-lite-preview` is known to work from the
previous audit cycle. Other model IDs in Phase 1's updated `_HARDCODED` are
based on official documentation; users should be aware these may not all be
accessible to every account tier (Free / AI Pro / API key) — this caveat is
documented in README and USAGE.

A re-verification pass should be scheduled after the Gemini quota resets.

## Wrapper conclusions

The wrapper itself **does not block any provider model** — `factory.route()`
plus per-provider `_build_args()` pass every model ID straight through to the
underlying CLI. User-reported "model not usable" issues all trace to:

1. **Cosmetic display** — `_HARDCODED` was missing newer Claude/Codex IDs,
   so `unified-cli models` didn't show them. Fixed in Phase 1.
2. **Bad IDs in our hardcoded list** — `gemini-3.1-pro` / `gemini-3.1-flash`
   were typos; the real IDs require the `-preview` suffix (or use the new
   stable `flash-lite` variant). Fixed in Phase 1.
3. **External factors**:
   - Outdated `codex` CLI blocks `gpt-5.5` (user upgrade required)
   - Gemini account quota (rate-limit; no wrapper change can help)

The Phase 1 commits update `_HARDCODED`, expose actual resolved model in
streaming events (so users see `opus → claude-opus-4-7`), and add safety
guards (timeouts, file permissions, empty-API-key handling, `is_error` raise,
broader error matchers).
