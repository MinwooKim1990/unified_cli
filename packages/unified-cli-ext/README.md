# unified-cli-ext

`unified-cli-ext` is the extension foundation for
[`unified-cli`](https://github.com/MinwooKim1990/unified_cli). Version 0.1.0
ships transport and runtime contracts plus an inert Stage 5B–5F catalog. It does
**not** include a working or enabled provider adapter.

## What this release does—and does not do

The package is intended for extension authors. It keeps provider identifiers
explicit and lazily resolved by Core. Installing it does not add a provider to
Core's built-in defaults, change Core's three built-in providers (Claude,
Codex, and Gemini/Antigravity), or change the local server allowlist. Server
exposure for extensions remains off.

The installed catalog has entry-point metadata for Grok, Kimi, Copilot,
Cursor, CodeBuddy, Qoder, Mistral Vibe, Qwen, Cline, OpenCode, Kilo Code,
Factory Droid, Pi, Oh My Pi, Hermes Agent, Poolside Agent CLI, Amp, and GitLab
Duo CLI. Exactly 18 inert entries are **Held**. The adapter metadata records `chat` only
as a provisional target; the Core plugin advertises no executable capability and
cannot construct a provider or execute a command. These are not working
adapters and must not be presented as Preview or Stable.

There are no bundled credentials, authentication flows, provider calls, or
paid-service calls in this release. Installation does not install vendor CLIs,
log in, call a service, or incur charges. Provider binaries and accounts stay
user-owned. Its validation uses offline fixtures only. Because no provider is
enabled, this release does not participate in vendor sign-in or request
handling and does not read or import account data.

Extensions are installed Python code and run as trusted code in the host
Python process when loaded.  Install only distributions you trust.

The local installation receipt API binds an explicitly selected executable or
npm launcher to observed file identity and metadata. It does not establish the
publisher's identity, so callers must still use the vendor's official
distribution channel and should verify the receipt immediately before launch.

## Requirements and installation

This distribution targets `unified-cli` 0.5.x.  Install it alongside a
compatible Core release:

```bash
python -m pip install "unified-cli~=0.5.0" unified-cli-ext
```

The import package is `unified_cli_ext`. No provider chat command, adapter
configuration, or authentication setup is documented because no live provider
is enabled. See the root [Extensions guide](https://github.com/MinwooKim1990/unified_cli/blob/main/docs/extensions.md) for the
Held catalog and official vendor documentation.

## Optional protocol dependencies

Protocol SDKs remain optional; they are not required to install the foundation
and do not enable provider calls in 0.1.0. The available extras are
`acp`, `mcp`, `all` (both protocol SDKs), and `dev` (test dependencies).

```bash
python -m pip install "unified-cli-ext[acp]"
python -m pip install "unified-cli-ext[mcp]"
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

## Status

This is a foundation release with an inert Held catalog, not a catalog of
supported external providers. Transport/runtime foundations are included, but
enabled provider adapters, provider-specific verified sessions, authentication,
and network-backed validation are deliberately out of scope for 0.1.0.
