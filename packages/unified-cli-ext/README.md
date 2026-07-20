# unified-cli-ext

`unified-cli-ext` is the Stage 2 extension foundation for
[`unified-cli`](https://github.com/MinwooKim1990/unified_cli).  Version 0.1.0
ships transport and contract building blocks only.  It does **not** include
working provider adapters.

## What this release does—and does not do

The package is intended for future extension authors.  It keeps future
extension identifiers explicit and lazily resolved by Core.  Installing it
does not add a provider to Core's built-in defaults, change Core's three
built-in providers (Claude, Codex, and Gemini/Antigravity), or change the
local server allowlist.  Server exposure for extensions remains off.

There are no bundled credentials, authentication flows, provider calls, or
paid-service calls in this release.  Its validation is fake/offline only.
It does not bypass authentication or rate limits, and it does not scrape,
collect, or recover credentials.

Extensions are installed Python code and run as trusted code in the host
Python process when loaded.  Install only distributions you trust.

## Requirements and installation

This distribution targets `unified-cli` 0.5.x.  Install it alongside a
compatible Core release:

```bash
python -m pip install "unified-cli~=0.5.0" unified-cli-ext
```

The import package is `unified_cli_ext`.  This initial release intentionally
does not document a provider command, adapter configuration, or authentication
setup because none is shipped.

## Optional protocol dependencies

Protocol SDKs are optional; they are not required to install the foundation
and are not used to make provider calls in 0.1.0.  The available extras are
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

## Scope and security posture

Core owns provider discovery and policy.  Future providers must be explicitly
requested; they are not selected by unprefixed model inference.  The Core HTTP
server continues to reject extension providers in this ABI stage, even if an
extension declares server-related metadata.

For the Core extension ABI and its trust boundary, see the
[provider plugin ABI](https://github.com/MinwooKim1990/unified_cli/blob/main/docs/development/provider-plugin-abi-v1.md).

## Status

This is a foundation release, not a catalog of supported external providers.
Provider adapters, real protocol sessions, authentication, and network-backed
validation are deliberately out of scope for 0.1.0.
