# Ext accountless live lab — 2026-07-23

This is disposable, credential-free evidence for the 18 `unified-cli-ext`
provider entry points. It is not authenticated provider certification.

## Isolation

- Image: `unified-ext-live-accountless:20260723`
- Base: `node:22-bookworm-slim`
- Container: `unified-ext-live-20260723`, non-root UID/GID 10001
- Read-only root filesystem, all capabilities dropped, `no-new-privileges`
- No host home, SSH, keychain, Docker socket, or credential mount
- Synthetic absolute Git repository: `/workspace/project`
- Provider installation used network access; every create/turn probe ran after
  the container network was disconnected.
- Each public API probe imported `unified_cli` and `unified_cli_ext`, confirmed
  passive discovery, explicitly loaded/snapshotted the provider, ran doctor,
  and called `from unified_cli import create` with the synthetic absolute cwd.
- Turn probes were externally bounded to 45 seconds and emitted only classified
  errors and hashes, never response or prompt content.

## Results

| Provider | Official install provenance | Observed version | `create(abs cwd)` | Accountless turn / blocker |
|---|---|---:|---|---|
| Amp | `@ampcode/cli` | `0.0.1784809706-g96cc8a` | pass | bounded `internal`; CLI phrase: `No API key found. Starting login flow...` |
| Cline | `cline` | `3.0.46` | pass | bounded `internal` |
| CodeBuddy | `@tencent-ai/codebuddy-code` | `2.126.0` | pass | CLI returned success without credentials; response content was not captured |
| Copilot | `@github/copilot` | `1.0.73` | pass | bounded `internal` |
| Cursor | `https://cursor.com/install` | `2026.07.20-8cc9c0b` | config blocker | official `cursor-agent` target is reached through the legacy `agent` alias; current receipt/version contract does not accept that calendar-version launcher |
| Droid | `https://app.factory.ai/cli` | `0.178.0` | pass | bounded `internal` |
| GitLab Duo | `@gitlab/duo-cli` | `9.6.0` | pass | `auth_expired` with official-login hint |
| Grok | `@xai-official/grok` | `0.2.111` | pass | bounded `internal` in this accountless lab; authenticated evidence is separate |
| Hermes | `https://hermes-agent.nousresearch.com/install.sh` (`--skip-setup --skip-browser`) | `0.19.0` | config blocker | official launcher uses an `env` shebang rejected by the direct-receipt security contract |
| Kilo | `@kilocode/cli` | `7.4.15` | pass | bounded `internal` |
| Kimi | `@moonshot-ai/kimi-code` | `0.29.0` | pass | bounded `internal` |
| Mistral Vibe | `uv tool install --python 3.12 mistral-vibe` | `2.22.0` | config blocker | official uv launcher is rejected by the canonical direct-receipt contract |
| Oh My Pi | `https://omp.sh/install` | `17.0.9` | pass | bounded `internal` |
| OpenCode | `opencode-ai` | `1.18.4` | pass | bounded `internal` |
| Pi | `npm --ignore-scripts @earendil-works/pi-coding-agent` | `0.81.1` | pass | bounded `internal`; RPC phrase: `No API key found for the selected model. Use /login to log into a provider via OAuth or API key.` |
| Poolside | `https://downloads.poolside.ai/pool/install.sh` | not installed | not run | installer required EULA acceptance; the lab did not accept an agreement on the user's behalf |
| Qoder | `@qoder-ai/qodercli` | `1.1.4` | config blocker | current public help no longer advertises the required ACP markers; the gate was not weakened |
| Qwen | `@qwen-code/qwen-code` | `0.20.1` | pass | bounded `internal` |

All 18 entry points passed passive discovery, explicit load, and descriptor
snapshot. Missing or incompatible launch contexts returned bounded `config`
errors without tracebacks, deadlocks, or host credential access.

## Compatibility changes supported by this run

- Npm launchers whose package target keeps the executable basename now receive
  an npm manifest receipt, while same-name direct-installer symlinks outside
  `node_modules` remain direct receipts.
- Plain-text version probes can require one unique non-empty full line, extract
  its first token, or require a literal suffix. Existing semver/minimum checks
  remain in force; Amp alone declares a larger component ceiling for its
  observed timestamp-like build component.
- Verified bare/current version formats were updated for Amp, Cline, CodeBuddy,
  Droid, GitLab Duo, Kilo, Kimi, Oh My Pi, OpenCode, Pi, Qoder, and Qwen.
  Copilot uses its exact `GitHub Copilot CLI <version>.` envelope.
- OpenCode and Kilo explicitly parse their verified help output from stderr.
  GitLab Duo, Pi, Oh My Pi, and OpenCode feature markers were narrowed to
  unique lines observed in current official help.
- The observed Amp phrase `No API key found` and Pi's fixed RPC no-key message
  now map to the Core-owned `auth_expired` category without reflecting vendor
  diagnostics. The table records the raw lab outcome before those mappings.

## Verification

- Focused provider/create/resolver suite: 42 tests passed.
- A broader provider-adapter file produced 161 passes, 1 skip, and 3 unrelated
  descendant-reaping failures caused by the container PID-namespace environment.
- The container and tagged lab image are removed after capture; cleanup checks
  filter only the exact lab label/name and image tag.
