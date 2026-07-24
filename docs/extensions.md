# Extensions

Extensions are bundled with Core in the `unified-cli` 0.5.3
distribution; `unified_cli` and `unified_cli_ext` are both public namespaces in
that one wheel. Core continues to support Claude, Codex, and Gemini (`agy`) as
its only defaults. Extensions are a feature boundary: using one does not change
those defaults, add an extension to Core's local server allowlist, or install or
configure vendor software.

The bundled extensions provide 18 explicit provider entry points and
executable **Preview** adapters. Grok Build is backed by offline fixtures and
one representative authenticated native smoke. Other providers use
fixture-tested common transports and are attempted only when explicitly
selected. Vendor CLI and account compatibility is not guaranteed. No Ext
provider is enabled in the public Core `/v1/*` server routes.

Vendor binaries, accounts, subscriptions, and their updates remain
user-owned. Installing Ext alone does not install a vendor CLI, log in, call a
service, or incur charges. Ext is not affiliated with the vendors listed here.

## Install and inspect

```bash
python -m pip install "unified-cli==0.5.3"
python -c "import unified_cli_ext; print(unified_cli_ext.__name__)"
unified-cli providers --include-ext
```

The Python command only confirms that the bundled extension namespace imports.
The `providers` command displays installed entry-point metadata. Neither
verifies a vendor installation, authentication state, or service availability.

For a developer or tester with a legacy local or failed split wheel, recover
before installing the planned unified release:

```bash
python -m pip uninstall -y unified-cli-ext
python -m pip install --force-reinstall "unified-cli==0.5.3"
```

Core keeps this discovery import-free. `unified-cli providers --include-ext`
reports the bundled entries as lifecycle `discovered` and support `preview`
without importing them. An explicit request loads only that provider's entry
point. All 18 entries have executable Preview adapters. The 2026-07-23
credential-free lab reached `create()` for 13 current official installations.
Cursor, Hermes, Mistral Vibe, and Qoder returned bounded compatibility errors;
Poolside was not installed because its installer required EULA acceptance.
See the [accountless lab evidence](development/ext-accountless-live-lab-2026-07-23.md).
Grok continues only after its explicitly selected local binary passes the
exact `0.2.111` version and bounded feature probes. Ext providers remain
disabled in public `/v1/*` routes.

## Grok Preview setup and boundary

The normal setup is short. `configure` verifies the official executable,
creates the reviewed isolated configuration, and stores a launch receipt. The
vendor's official login then writes only to that isolated home:

```bash
curl -fsSL https://x.ai/cli/install.sh | bash
unified-cli configure grok
HOME="$HOME/.unified-cli/providers/grok/home" grok login --device-code
python examples/09_extensions.py grok "Explain this repository"
```

The same flow is available from Python:

```python
from pathlib import Path
from unified_cli import configure_extension_provider, create

configure_extension_provider("grok")
client = create("grok", cwd=str(Path.cwd().resolve()))
print(client.chat("Explain this repository").text)
```

The normal user `~/.grok` authentication is never copied. If login is missing,
Python and the CLI return a sanitized `auth_expired` error with the isolated
login instruction.

<details>
<summary>Manual native snapshot verification reference</summary>

The following longer recipe documents the checks performed automatically and
is intended for maintainers auditing the reviewed macOS arm64 snapshot.

Grok Build Preview is one runnable Ext provider. Its primary official
native installer is `https://x.ai/cli/install.sh`; the official npm package
`@xai-official/grok` is a vendor alternative, but this 0.1 Preview setup uses
the native install layout only. The known unrelated
`@vibe-kit/grok-cli` CLI shape is rejected. The only reviewed version is
`0.2.111`; other versions fail closed. The only verified platform is macOS
arm64. Run the vendor installer in your normal user home, then use the
fail-closed recipe below. It accepts only the native installer's owned,
non-writable `~/.grok` layout, requires its `bin/grok` symlink to resolve
directly to a single-link regular file in `downloads`, verifies the exact
reviewed SHA-256, and copies it into a fresh version/platform-qualified private
snapshot. It also creates the exact safe configuration before any login.
Unsupported OS/architecture, path shape, ownership, link type/count, mode,
digest, or version output is refused:

```bash
curl -fsSL https://x.ai/cli/install.sh | bash
python -I - <<'PY'
import hashlib
import os
import platform
import re
import stat
import subprocess
import sys
from pathlib import Path
from unified_cli_ext.providers.grok import GROK_FIXED_ENVIRONMENT, GROK_SAFE_CONFIG

DIGEST = "e1fafdfffe14f339460befaf194360e8f90bfd02efe8a4f24cfa1c7aea657ffe"
VERSION = b"0.2.111"
SNAPSHOT = "native-0.2.111-darwin-arm64"
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)

if sys.platform != "darwin" or platform.machine().lower() != "arm64":
    raise SystemExit("verified only on macOS arm64")
if not NOFOLLOW or not DIRECTORY:
    raise SystemExit("required no-follow operations are unavailable")

def directory(parent, name, *, create=False, private=True):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
    meta = os.stat(name, dir_fd=parent, follow_symlinks=False)
    mode = stat.S_IMODE(meta.st_mode)
    if (
        not stat.S_ISDIR(meta.st_mode)
        or meta.st_uid != os.getuid()
        or mode & 0o022
        or (private and mode != 0o700)
    ):
        raise SystemExit(f"refusing unsafe directory {name!r}")
    return os.open(name, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=parent)

def sha256(fd):
    value = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while block := os.read(fd, 1024 * 1024):
        value.update(block)
    os.lseek(fd, 0, os.SEEK_SET)
    return value.hexdigest()

def environment(home, tmp):
    # GROK_FIXED_ENVIRONMENT includes GROK_MANAGED_MCPS_ENABLED=false,
    # GROK_MANAGED_MCP_GATEWAY_TOOLS_ENABLED=false, and GROK_RESPECT_GITIGNORE=1.
    return {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TMPDIR": str(tmp),
        **dict(GROK_FIXED_ENVIRONMENT),
    }

def require_version(binary, cwd, env):
    result = subprocess.run(
        [str(binary), "--version"],
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    match = re.fullmatch(
        rb"grok ([0-9]+\.[0-9]+\.[0-9]+)(?: [^\r\n]*)?\r?\n?",
        result.stdout,
    )
    if (
        result.returncode != 0
        or len(result.stdout) > 4096
        or match is None
        or match.group(1) != VERSION
    ):
        raise SystemExit("refusing unexpected Grok version output")

home_path = Path.home()
home_fd = os.open(home_path, os.O_RDONLY | DIRECTORY | NOFOLLOW)
home_meta = os.fstat(home_fd)
if (
    not stat.S_ISDIR(home_meta.st_mode)
    or home_meta.st_uid != os.getuid()
    or stat.S_IMODE(home_meta.st_mode) & 0o022
):
    raise SystemExit("refusing unsafe home directory")

vendor_fd = directory(home_fd, ".grok", private=False)
vendor_bin_fd = directory(vendor_fd, "bin", private=False)
downloads_fd = directory(vendor_fd, "downloads", private=False)
alias = os.stat("grok", dir_fd=vendor_bin_fd, follow_symlinks=False)
if not stat.S_ISLNK(alias.st_mode) or alias.st_uid != os.getuid():
    raise SystemExit("refusing unexpected ~/.grok/bin/grok")
link = os.readlink("grok", dir_fd=vendor_bin_fd)
source = Path(os.path.abspath(os.path.join(home_path, ".grok", "bin", link)))
downloads = home_path / ".grok" / "downloads"
if (
    source.parent != downloads
    or re.fullmatch(r"grok-[A-Za-z0-9._-]+", source.name) is None
):
    raise SystemExit("refusing unexpected Grok download path")
source_fd = os.open(source.name, os.O_RDONLY | NOFOLLOW, dir_fd=downloads_fd)
source_meta = os.fstat(source_fd)
if (
    not stat.S_ISREG(source_meta.st_mode)
    or source_meta.st_uid != os.getuid()
    or source_meta.st_nlink != 1
    or not (source_meta.st_mode & stat.S_IXUSR)
    or stat.S_IMODE(source_meta.st_mode) & 0o022
    or sha256(source_fd) != DIGEST
):
    raise SystemExit("refusing unsafe or unreviewed Grok download")

os.umask(0o077)
unified_fd = directory(home_fd, ".unified-cli", create=True)
providers_fd = directory(unified_fd, "providers", create=True)
grok_fd = directory(providers_fd, "grok", create=True)
try:
    os.mkdir(SNAPSHOT, 0o700, dir_fd=grok_fd)
    os.fsync(grok_fd)
except FileExistsError as exc:
    raise SystemExit("snapshot already exists; refusing reuse") from exc
root_fd = directory(grok_fd, SNAPSHOT)
bin_fd = directory(root_fd, "bin", create=True)
provider_home_fd = directory(root_fd, "home", create=True)
state_fd = directory(provider_home_fd, ".grok", create=True)
login_fd = directory(root_fd, "login-cwd", create=True)
tmp_fd = directory(root_fd, "tmp", create=True)

target_fd = os.open(
    "grok",
    os.O_RDWR | os.O_CREAT | os.O_EXCL | NOFOLLOW,
    0o500,
    dir_fd=bin_fd,
)
target_meta = os.fstat(target_fd)
if (
    not stat.S_ISREG(target_meta.st_mode)
    or target_meta.st_uid != os.getuid()
    or target_meta.st_nlink != 1
    or stat.S_IMODE(target_meta.st_mode) != 0o500
):
    raise SystemExit("refusing unsafe snapshot binary")
while block := os.read(source_fd, 1024 * 1024):
    while block:
        written = os.write(target_fd, block)
        block = block[written:]
os.fsync(target_fd)
os.fsync(bin_fd)
if sha256(target_fd) != DIGEST:
    raise SystemExit("snapshot digest verification failed")

config = GROK_SAFE_CONFIG.encode("utf-8")
config_fd = os.open(
    "config.toml",
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW,
    0o600,
    dir_fd=state_fd,
)
config_meta = os.fstat(config_fd)
if (
    not stat.S_ISREG(config_meta.st_mode)
    or config_meta.st_uid != os.getuid()
    or config_meta.st_nlink != 1
    or stat.S_IMODE(config_meta.st_mode) != 0o600
):
    raise SystemExit("refusing unsafe config target")
while config:
    written = os.write(config_fd, config)
    config = config[written:]
os.fsync(config_fd)
os.fsync(state_fd)

root = home_path / ".unified-cli" / "providers" / "grok" / SNAPSHOT
binary = root / "bin" / "grok"
provider_home = root / "home"
login_cwd = root / "login-cwd"
tmp = root / "tmp"
if os.listdir(login_fd):
    raise SystemExit("refusing non-empty login cwd")
fixed_environment = environment(provider_home, tmp)
require_version(binary, login_cwd, fixed_environment)
os.fchdir(login_fd)
os.execve(
    str(binary),
    [str(binary), "login", "--device-auth"],
    fixed_environment,
)
PY
```

Do not reuse a generic host login. The single setup invocation creates the
config and copied binary with no-follow exclusive opens, verifies their
single-link regular metadata, fsyncs both, rechecks the copied digest and exact
parsed `0.2.111` version, confirms the login cwd is empty, then `execve`s the
fixed login argv with a minimal fixed environment. It never truncates or
replaces a pre-existing snapshot or config, follows a pre-existing path, uses
shell interpolation, inspects auth contents, or prints credentials. If the
qualified snapshot already exists, setup refuses rather than silently reusing
it; use the already-authenticated snapshot or inspect/remove it manually before
an intentional fresh setup.
After authenticating that snapshot, register the same canonical binary and home
through Core's public extension-configuration API:

```bash
python - <<'PY'
from pathlib import Path
from unified_cli import ExtensionLaunchOverridesV1, configure_extension_provider

root = (
    Path.home()
    / ".unified-cli"
    / "providers"
    / "grok"
    / "native-0.2.111-darwin-arm64"
)
configure_extension_provider(
    "grok",
    ExtensionLaunchOverridesV1(
        bin_path=str(root / "bin" / "grok"),
        provider_home=str(root / "home"),
    ),
)
PY
unified-cli chat "explain this project" --provider grok --model grok-4.5
```

These are copy-only instructions; Ext never runs installation or login itself.
Repeat the snapshot and registration after an intentional vendor CLI update.

For each prompt, the adapter fixes `--no-auto-update`, strict sandboxing,
`dontAsk`, and permits only `read_file`, `grep`, and `list_dir`. Its fixed
process environment disables the updater, write, tool-search, LSP, memory,
subagents, web, and Claude/Cursor/Codex skills, rules, agents, MCPs, hooks, and
sessions, and requires gitignore-aware traversal. Managed MCP and official
marketplace auto-registration are off, and marketplace packages require a SHA;
callers cannot override these controls.
Immediately before every turn it fails closed if it finds
`.grok`, `.envrc`, `.mcp.json`, `.cursor/mcp.json`,
`.cursor/hooks.json`, or `.claude` from the cwd up to the Git root (or at the
cwd only when it is outside a Git repository);
provider-home shell startup files, runtime config (except the exact safe `0600`
template above), plugins, hook directories or hook-path files; or managed
`/etc/grok` configuration. The provider home must be an owned real `0700`
directory; `.grok` must be an owned real directory without group/other write,
and config/auth state must be private regular single-link files. Auth contents
are never parsed by this validation. An otherwise isolated provider `HOME` with
the safe template and vendor runtime state is allowed.

These controls are defense in depth around a read-only Preview, not a complete
secret boundary. Gitignore-aware traversal reduces accidental exposure but does
not make ignored or workspace-readable files secret from the vendor process.

Offline fixtures verify the adapter. A representative isolated device-code
smoke of official native Grok `0.2.111` (commit marker `94172f2aa4e5`) passed on
macOS arm64 on 2026-07-23; its sanitized results are recorded in
[the smoke evidence](development/grok-0.2.111-smoke.md). This is one
version/platform/auth sample, not a release-wide compatibility claim. Grok is
therefore still Preview, not Stable, and remains disabled in public `/v1/*`
server routes.

</details>

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

All 18 catalog entries below have executable **Preview** adapters and are
attempted when explicitly selected. The current accountless lab reached
`create()` for 13/18; its four compatibility blockers and one EULA-limited
installation are documented above. Grok has representative authenticated
live-test evidence; other providers have fixture-tested common transports,
not a guarantee of vendor or account compatibility. All Ext public-server policies are disabled, so
public-compatible `/v1/*` routes remain Core-only. The loopback-only
`serve --manage` UI may still invoke an explicitly selected Ext provider in a
registered workspace through the same Python `create()` path.

OpenCode Go was also tested with an authenticated account on 2026-07-24. Its
official CLI passed model refresh, chat, local tools, web search, and a
synthetic image, but unified-cli failed provenance binding for the official
Homebrew installation across Python, REPL, and Browser surfaces. OpenCode
therefore remains Preview. See the
[live smoke matrix](development/opencode-go-live-smoke-2026-07-24.md).

## Generated provider support

The machine-status table below is generated from the explicit Ext entry-point
plugins. The detailed candidate-transport catalog that follows remains a
manual design record.

<!-- BEGIN GENERATED EXT PROVIDER SUPPORT -->
| Provider ID | Support status | Core capabilities | Server |
|---|---|---|---|
| `amp` | `preview` | `chat` | `disabled` |
| `cline` | `preview` | `chat` | `disabled` |
| `codebuddy` | `preview` | `chat` | `disabled` |
| `copilot` | `preview` | `chat` | `disabled` |
| `cursor` | `preview` | `chat` | `disabled` |
| `droid` | `preview` | `chat, stream` | `disabled` |
| `gitlab-duo` | `preview` | `chat` | `disabled` |
| `grok` | `preview` | `chat, sessions, stream` | `disabled` |
| `hermes` | `preview` | `chat` | `disabled` |
| `kilo` | `preview` | `chat` | `disabled` |
| `kimi` | `preview` | `chat` | `disabled` |
| `mistral-vibe` | `preview` | `chat` | `disabled` |
| `oh-my-pi` | `preview` | `chat, stream` | `disabled` |
| `opencode` | `preview` | `chat` | `disabled` |
| `pi` | `preview` | `chat, stream` | `disabled` |
| `poolside` | `preview` | `chat` | `disabled` |
| `qoder` | `preview` | `chat` | `disabled` |
| `qwen` | `preview` | `chat` | `disabled` |
<!-- END GENERATED EXT PROVIDER SUPPORT -->

## Historical Stage 5B–5F design catalog

“Candidate transport” records a provisional design direction, not a command
contract. This is retained as a pre-0.5.2 design record; its historical Held
and Experimental labels are superseded by the generated support table above.
Every current adapter is executable only after explicit selection; Preview
compatibility can still stop before a turn with a bounded configuration error.

The Grok, Kimi, Copilot, and Cursor rows record current official-documentation
research and pinned compatibility targets. Prompts are argv values and must not
be logged. Grok has an offline-fixture-verified one-shot bridge and one
representative authenticated native smoke. The other providers are Preview
adapters with common transports validated by fixtures; their vendor-specific
CLI and account combinations have not all been live-tested.

| Provider ID | Official binary/package | Candidate transport | Provisional adapter target | Historical pre-0.5.2 status | Auto-update containment | Official documentation |
|---|---|---|---|---|---|---|
| `grok` | xAI Grok Build (`grok`): primary native installer `https://x.ai/cli/install.sh`; official npm `@xai-official/grok` is alternative; rejects unrelated `@vibe-kit/grok-cli` CLI shape | Streaming JSONL | Explicit `-p` one-shot with `chat`, `stream`, `sessions`; exact `0.2.111`; default `grok-4.5` | Preview | Fixed no-auto-update, strict sandbox, `dontAsk`, write/tool-search/LSP/memory/subagents/web, compatibility scanners, managed MCP, marketplace auto-registration off; marketplace SHA and gitignore-aware traversal required; exact private safe config and fail-closed workspace/home/system preflight; defense in depth, not a complete secret boundary; offline fixtures plus one representative authenticated native `0.2.111` macOS arm64 smoke | [Repository](https://github.com/xai-org/grok-build) · [Overview](https://docs.x.ai/build/overview) · [CLI reference](https://docs.x.ai/build/cli/reference) · [Headless scripting](https://docs.x.ai/build/cli/headless-scripting) |
| `kimi` | Kimi Code CLI (`kimi`, `@moonshot-ai/kimi-code`), not legacy Python `kimi-cli` | Stream JSON candidate | `-p` one-shot auto-approves normal tools; Core capability none | Held | Candidate `KIMI_CODE_NO_AUTO_UPDATE=1` and `KIMI_DISABLE_TELEMETRY=1`; no per-run read-only/no-tools/web-off/MCP-off contract | [Getting started](https://moonshotai.github.io/kimi-code/en/guides/getting-started.html) · [Kimi command](https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html) · [Kimi ACP](https://moonshotai.github.io/kimi-code/en/reference/kimi-acp.html) |
| `copilot` | GitHub Copilot CLI (`copilot`, `@github/copilot`) | Plain-text one-shot candidate | Explicit read-only tool candidate; Core capability none | Held | Candidate `--no-auto-update` plus tool/MCP controls; JSONL schema and complete user/workspace MCP and dedicated-home isolation remain unverified | [Install](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli) · [CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference) · [ACP server](https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server) |
| `cursor` | Cursor Agent CLI (`agent` primary; `cursor-agent` legacy alias since 2026-01-08) | Final/stream JSON schema candidates | Positional-prompt ABI is not safely representable; Core capability none | Held | No verified read-only, MCP, or update containment; `CURSOR_API_KEY` is env-only and never argv | [Install](https://cursor.com/docs/cli/installation) · [Parameters](https://cursor.com/docs/cli/reference/parameters) · [Output format](https://cursor.com/docs/cli/reference/output-format) · [ACP](https://cursor.com/docs/cli/acp) |
| `codebuddy` | CodeBuddy Code (`codebuddy`, `@tencent-ai/codebuddy-code`) | JSONL protocol candidate | `chat` candidate; Core capability none | Held | Candidate `DISABLE_AUTOUPDATER=1`; exact frame and config isolation require verification | [CLI reference](https://www.codebuddy.ai/docs/cli/cli-reference) · [Headless mode](https://www.codebuddy.ai/docs/cli/headless) · [ACP](https://www.codebuddy.ai/docs/cli/acp) |
| `qoder` | Qoder CLI (`qodercli`, `@qoder-ai/qodercli`) | ACP stdio | Explicit `chat`; Core server disabled | Experimental | Private setting `general.enableAutoUpdate=false`; current `1.1.4` help no longer exposes the required ACP probe markers, so the lab returns a bounded config error | [Quick start](https://docs.qoder.com/en/cli/quick-start) · [ACP](https://docs.qoder.com/en/cli/acp) · [Permissions](https://docs.qoder.com/en/cli/permissions) |
| `mistral-vibe` | Mistral Vibe (`vibe`, `mistral-vibe`) | JSONL message stream candidate | `chat` candidate; Core capability none | Held | Candidate private config with update checks off; direct and `vibe-acp` paths require separate verification | [Install](https://docs.mistral.ai/getting-started/quickstarts/vibe-code/install-cli) · [CLI workflow](https://docs.mistral.ai/vibe/code/cli/work-with-cli) · [ACP surfaces](https://docs.mistral.ai/vibe/code/choose-cli-vscode-web-sessions) |
| `qwen` | Qwen Code (`qwen`, `@qwen-code/qwen-code`) | JSONL candidate | `chat` candidate; Core capability none | Held | Backend selection, credentials, update behavior, and event schema require verification | [Repository](https://github.com/QwenLM/qwen-code) · [Headless mode](https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/) · [Authentication](https://qwenlm.github.io/qwen-code-docs/en/users/configuration/auth/) |
| `cline` | Cline CLI (`cline`) | JSONL candidate; separate ACP candidate | `chat` candidate; Core capability none | Held | Candidate `CLINE_NO_AUTO_UPDATE=1`; stdin EOF, event schema, and local configuration isolation require verification | [CLI overview](https://docs.cline.bot/usage/cli-overview) · [CLI reference](https://docs.cline.bot/cli/cli-reference) · [Release source](https://github.com/cline/cline/tree/cli-v3.0.46/apps/cli) |
| `opencode` | OpenCode (`opencode`, package `opencode-ai`) | `JSONL one-shot` candidate | `chat` candidate; Core capability none | Held | 2026-07-24 authenticated Go smoke passed the vendor CLI directly, but the current adapter rejects the official Homebrew Cellar directory chain during provenance binding; Python/REPL calls fail, Browser Verify/Models return HTTP 502, Browser Chat is disabled, and model/image/web/tool/session forwarding remains incomplete | [Documentation](https://opencode.ai/docs/) · [CLI](https://opencode.ai/docs/cli/) · [Server](https://opencode.ai/docs/server/) · [Live matrix](development/opencode-go-live-smoke-2026-07-24.md) |
| `kilo` | Kilo Code (`kilo`, package `@kilocode/cli`) | `ACP stdio with an internal loopback server` | Explicit `chat`; Core server disabled | Experimental | Runnable with bounded ACP loopback/process/config/permission controls; behavior may change | [CLI](https://kilo.ai/docs/code-with-ai/platforms/cli) · [CLI reference](https://kilo.ai/docs/code-with-ai/platforms/cli-reference) · [Release](https://github.com/Kilo-Org/kilocode/releases/tag/v7.4.11) |
| `droid` | Factory Droid (`droid`, npm package `droid`) | Vendor stream JSON-RPC candidate | `chat` candidate; Core capability none | Held | Candidate update control, protocol envelope, permission flow, and process lifecycle require Stage 6 verification | [CLI reference](https://docs.factory.ai/reference/cli-reference) · [Droid Exec](https://docs.factory.ai/cli/droid-exec/overview) · [Package metadata](https://registry.npmjs.org/droid/latest) |
| `pi` | Pi Coding Agent (`pi`, package `@earendil-works/pi-coding-agent`) | Custom NDJSON RPC candidate | `chat` candidate; Core capability none | Held | Candidate `--offline` and resource-disable flags require Stage 6 verification; this is not JSON-RPC | [Package](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/package.json) · [README](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md) · [RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md) |
| `oh-my-pi` | Oh My Pi (`omp`, package `@oh-my-pi/pi-coding-agent`) | Custom NDJSON RPC candidate | `chat` candidate; Core capability none | Held | No verified update containment claim yet; configuration, resources, permissions, and process lifecycle require Stage 6 verification | [Repository](https://github.com/can1357/oh-my-pi) · [RPC](https://github.com/can1357/oh-my-pi/blob/main/docs/rpc.md) · [Approval mode](https://github.com/can1357/oh-my-pi/blob/main/docs/approval-mode.md) |
| `hermes` | Hermes Agent (`hermes`, PyPI `hermes-agent[acp]`) | ACP stdio candidate | `chat` candidate; Core capability none | Held | Hermes pins ACP 0.9.0 while Ext targets 0.11.x; compatibility, configuration, and lifecycle require Stage 6 verification | [PyPI](https://pypi.org/project/hermes-agent/) · [Repository](https://github.com/NousResearch/hermes-agent) · [ACP guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/acp.md) |
| `poolside` | Poolside Agent CLI (`pool`, official native release) | ACP stdio | Explicit `chat`; Core server disabled | Experimental | Adapter and fixtures exist; the live lab did not accept the proprietary installer's EULA, so current binary creation was not tested | [Install](https://docs.poolside.ai/cli/install) · [CLI reference](https://docs.poolside.ai/cli/cli-reference) · [Release](https://github.com/poolsideai/pool/releases/tag/v1.0.13) |
| `amp` | Amp CLI (`amp`, canonical package `@ampcode/cli`) | Claude-compatible streaming JSONL input/output candidate | `chat` candidate; Core capability none | Held | Tool approval is off by default; workspace settings, plugins, MCP, EOF/process lifecycle, and paid opt-in execution require isolated Stage 6 evidence | [Manual](https://ampcode.com/manual) · [Stream schema](https://ampcode.com/manual/appendix) · [Package](https://www.npmjs.com/package/@ampcode/cli) |
| `gitlab-duo` | GitLab Duo CLI (`duo`, compiled generic package or official npm package `@gitlab/duo-cli`) | One-shot JSON candidate | `chat` candidate; Core capability none | Held | Headless runs auto-approve tools; JSON schema 1.0, authentication/entitlement, context/MCP/hooks, cleanup, and isolation require Stage 6 evidence | [Overview](https://docs.gitlab.com/user/gitlab_duo_cli/) · [Usage](https://docs.gitlab.com/user/gitlab_duo_cli/use/) · [Setup](https://docs.gitlab.com/user/gitlab_duo_cli/set_up/) |

The optional `acp` and `mcp` extras install protocol SDK dependencies only:
`unified-cli[acp]` and `unified-cli[mcp]`. They do not activate another
provider or make a provider call.

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
promoted to Stable. Until it is complete and reviewed, the current runnable
entry remains Preview.

## Trust and ownership boundary

Extensions are installed Python code and run as trusted code in the host
Python process when loaded. Install only distributions you trust. Core owns
provider discovery and policy: extension providers must be explicitly
requested and are not selected by unprefixed model inference. The Core HTTP
server continues to reject extension providers at this stage.

For the Core extension ABI and trust boundary, see the
[provider plugin ABI](development/provider-plugin-abi-v1.md).
