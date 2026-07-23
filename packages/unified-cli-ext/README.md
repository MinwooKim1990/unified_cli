# Extensions source tree

This directory organizes the extension source included in
[`unified-cli`](https://github.com/MinwooKim1990/unified_cli). For the planned
0.5.1 release it is not independently buildable or installable: one
`unified-cli` wheel provides both `unified_cli` and `unified_cli_ext`. The
extension feature set ships transport and runtime contracts, one bounded Grok
Build Preview adapter, three runnable Experimental adapters (Qoder, Kilo, and
Poolside), and 14 inert Held catalog entries.

## What this release does—and does not do

The package is intended for extension authors. It keeps provider identifiers
explicit and lazily resolved by Core. Installing it does not add a provider to
Core's built-in defaults, change Core's three built-in providers (Claude,
Codex, and Gemini/Antigravity), or change the local server allowlist. Server
exposure for extensions remains off.

The installed catalog has entry-point metadata for Grok, Kimi, Copilot,
Cursor, CodeBuddy, Qoder, Mistral Vibe, Qwen, Cline, OpenCode, Kilo Code,
Factory Droid, Pi, Oh My Pi, Hermes Agent, Poolside Agent CLI, Amp, and GitLab
Duo CLI. Grok is a read-tool-limited **Preview** with `chat`, `stream`, and
`sessions` capabilities. Qoder, Kilo, and Poolside are runnable **Experimental**
`chat` integrations. The other 14 entries are **Held**: their Core plugins
advertise no executable capability and cannot construct a provider or execute a
command. All extension server policies are disabled.

For Grok Build, Kimi Code CLI, GitHub Copilot CLI, and Cursor Agent CLI, the
catalog now binds official source links, frozen future-lab targets, documented
command candidates, and explicit remaining Stage 6 evidence gates. Those are
research records, not authenticated provider captures. The Grok adapter accepts
only the documented official CLI shape, rejects the known unrelated
`@vibe-kit/grok-cli` shape, disables auto-update/web/plan/subagents/memory, and
exposes only `read_file`, `grep`, and `list_dir`. Offline fixtures cover exact
argv construction, stream/session normalization, malformed output, cancellation,
and output limits. One representative isolated device-code smoke of official
native Grok `0.2.111` passed on macOS arm64 on 2026-07-23. It remains Preview
and server-disabled because that is one version/platform/auth sample. Kimi `-p` auto-approves normal tools;
Copilot still lacks the required local provenance capture; and Cursor still
needs its positional prompt and configuration boundaries verified. Those three
and every remaining catalog entry stay Held; Qoder, Kilo, and Poolside are the
separate runnable Experimental integrations.

There are no bundled credentials, login flows, or paid-service calls in this
release. Installation does not install vendor CLIs, log in, call a service, or
incur charges. Provider binaries and accounts stay user-owned. Grok calls occur
only after the user explicitly selects `grok`; passive discovery performs no
probe or provider call.

Extensions are installed Python code and run as trusted code in the host
Python process when loaded.  Install only distributions you trust.

The local installation receipt API binds an explicitly selected executable or
npm launcher to observed file identity and metadata. It does not establish the
publisher's identity, so callers must still use the vendor's official
distribution channel and should verify the receipt immediately before launch.

## Requirements and installation

Install the planned unified release; Core and extensions are a feature boundary,
not two distributions:

```bash
python -m pip install "unified-cli==0.5.1"
```

If a developer or tester installed a legacy local or failed split wheel, first
run `python -m pip uninstall -y unified-cli-ext`, then run
`python -m pip install --force-reinstall "unified-cli==0.5.1"`. No separate
project was published to public PyPI.

The import package is `unified_cli_ext`. Before selecting Grok, complete the
official-native-binary snapshot, isolated login, and
`configure_extension_provider(...)` registration in the root
[Extensions guide](https://github.com/MinwooKim1990/unified_cli/blob/main/docs/extensions.md).
Then the Preview can be selected explicitly:

```bash
unified-cli chat "explain this project" --provider grok --model grok-4.5
```

The 0.1 Preview setup uses the native layout installed by
`https://x.ai/cli/install.sh`; `@xai-official/grok` is an official vendor
alternative but is not registered by that setup recipe, while
`@vibe-kit/grok-cli` is rejected. Require exactly Grok `0.2.111`; unreviewed
patch and minor versions fail closed. The default and only model observed in
the representative smoke was `grok-4.5`.
Authentication uses an isolated provider `HOME`, not an assumed reusable host
login, and requires the exact private (`0600`) safe config template before
login; a missing config or other provider config is rejected. The fixed adapter
boundary disables auto-update, write, tool search, LSP, plan, subagents, memory,
web, managed MCP, official marketplace auto-registration, and
Claude/Cursor/Codex skills, rules, agents, MCPs, hooks, and sessions;
marketplace packages require a SHA and traversal must respect gitignore. It
uses strict sandbox and `dontAsk`, and permits only `read_file`, `grep`, and
`list_dir`.

The adapter may read files in the selected working directory and the vendor CLI
may maintain its own account/configuration files. Use it only in a trusted
workspace. These controls are defense in depth, not a complete secret boundary;
gitignore does not make readable files secret from the vendor process. See the
root [Extensions guide](https://github.com/MinwooKim1990/unified_cli/blob/main/docs/extensions.md)
for the full catalog and official vendor documentation.

## Optional protocol dependencies

Protocol SDKs remain optional; they are not required for Core or Grok in
`unified-cli` 0.5.1. The `acp` extra is required when explicitly using the
Qoder, Kilo, or Poolside Experimental integrations, but installing it does not
select or run a provider by itself. The available extras are `acp`, `mcp`,
`all` (both protocol SDKs), and `dev` (test dependencies).

```bash
python -m pip install "unified-cli[acp]"
python -m pip install "unified-cli[mcp]"
```

- ACP support uses the official
  [`agent-client-protocol`](https://github.com/agentclientprotocol/python-sdk)
  Python package, constrained to `>=0.11,<0.12`.  Its current 0.11.0 release
  requires Python 3.10 or later; this extra is declared for Python 3.10–3.14.
- MCP support targets the official stable v1 Python SDK,
  [`mcp`](https://github.com/modelcontextprotocol/python-sdk), constrained as
  `mcp>=1.27,<2` while v2 compatibility is evaluated.  This extra requires
  Python 3.10 or later.

## Runtime boundary

Core owns provider discovery and policy.  Future providers must be explicitly
requested; they are not selected by unprefixed model inference.  The Core HTTP
server continues to reject extension providers in this ABI stage, even if an
extension declares server-related metadata.

For the Core extension ABI and its trust boundary, see the
[provider plugin ABI](https://github.com/MinwooKim1990/unified_cli/blob/main/docs/development/provider-plugin-abi-v1.md).

### Explicit probe caches

`ProviderAdapterV1` starts with empty caches and populates them only when a
caller explicitly invokes `inspect`, `authenticated`, or `list_models`.
Successful immutable records have separate bounded monotonic TTLs: five
minutes for inspection, 15 seconds for authentication status, and one minute
for non-empty model lists. Every hit first re-verifies the cheap
`BinaryProvenance` metadata;
replacement of the exact executable invalidates its probe records. Account-
sensitive keys also include the validated provider-home directory identity and
digests of the adapter's selected environment values. Raw secrets are not cache
keys or values.

These APIs do not expose a provider-supplied account identifier, so the cache
does not claim to identify an account beyond that home/environment boundary.
An account changed by an external process can remain represented until the
short auth TTL expires. Adapter login/logout preparation invalidates auth and
model records for that context. Callers can request `force_refresh=True` on
the three probe methods or call `invalidate_cache()` explicitly. Exceptions,
cancellations, and permission failures are not retained as successful cache
records, and none of this imports a plugin or probes a provider at startup.

## Status

This is a foundation release with one offline-fixture-verified Grok Preview,
three runnable Experimental integrations (Qoder, Kilo, and Poolside), and 14
Held entries. A representative authenticated native smoke has passed, but it
does not establish broad provider compatibility; Grok is not Stable. No Ext
provider is exposed through Core's local server.
