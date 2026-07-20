# Platform-next Stage 2 audit

Recorded on 2026-07-20 from the `codex/platform-ext-foundation` feature
branch, based on Stage 1 integration commit
`9cb97ac9db485f5e3f6b28ac61f69efbcbd1797c`.

## Scope

Stage 2 establishes the separately distributable `unified-cli-ext` package and
the adapter-neutral execution contracts required by later provider releases.
It intentionally contains no provider adapter or provider entry point.

- Core version: `unified-cli 0.5.0`
- Ext version: `unified-cli-ext 0.1.0`
- Ext import namespace: `unified_cli_ext`
- Sole mandatory Ext dependency: `unified-cli>=0.5,<0.6`
- Optional SDK extras: ACP and MCP
- Required platform for subprocess transports: POSIX

Core does not import Ext and Ext never writes into the `unified_cli` package.
The Ext wheel contains only `unified_cli_ext` and its own distribution metadata.
The Core-only, Core-plus-Ext, and Ext-removal combinations are independently
tested.

The foundation includes bounded JSONL, bidirectional JSON-RPC, loopback-only
HTTP/SSE, lazy official ACP SDK loading, an allowlisted MCP callable bridge,
immutable normalized events, partial/final text de-duplication, strict tool-ID
correlation, provider-namespaced sessions, default-deny permissions, isolated
subprocess environments, monotonic deadlines, cancellation, and POSIX process
group cleanup.

## Security and compatibility boundary

- Subprocess arguments are arrays; shell strings are not accepted.
- JSON input rejects duplicate keys, non-finite numbers, excessive depth,
  excessive members, oversized strings, and signed-64-bit overflow.
- A provider receives only a minimal base environment plus explicitly
  allowlisted provider variables. Other provider credentials are not inherited.
- Temporary HOME and TMPDIR values are used in this fixture-only foundation.
- Loopback HTTP requires an IP literal, pins the exact origin, bounds redirects,
  headers, lines, events, and bodies, and refuses cross-origin redirects.
- SSE accepts CRLF, LF, and CR line endings and one initial UTF-8 BOM while
  retaining all byte, line, event, deadline, and cancellation limits.
- Tool and permission state is validated before correlation state mutates.
- Raw reasoning event kinds are discarded and reasoning-like object keys are
  removed from normalized payloads.
- Optional SDK import and factory failures do not expose exception causes or
  third-party diagnostics.
- JSON-RPC reverse callbacks are bounded from the transport perspective. A
  non-cooperative synchronous Python callback cannot be forcibly killed, so it
  is abandoned only in a daemon worker after provider process cleanup.
- Ext capabilities cannot grant Core server exposure or permission approval.

## Independent Sol audit

The first audit pass found two P1 and four P2 groups: a blocking stdin write
could outlive the operation timeout, arbitrary optional-SDK import exceptions
could escape, hostile mappings and rejected tool events could violate normalizer
state, scalar input edges escaped stable errors, and the Ext publish workflow
did not invoke the shared wheel-pair verifier.

After those repairs, the same independent reviewer found and verified a second
set of boundary cases: blocking reverse callbacks, JSON-RPC params and error
shape compliance, source-only tests in the Core sdist, Unicode format-control
spoofing, huge-number conversion, bare-string MCP allowlists, and CR-only/BOM
SSE framing. Each issue was fixed and converted to a regression test.

The reviewer then re-read the final snapshot and reported:

- P0: **0**
- P1: **0**
- P2: **0**
- Gate: **PASS**
- Stage 2 may advance: **yes**

## Verification

- Core source suite: **391 passed, 1 existing Starlette warning**
- Ext source suite: **88 passed**
- Extracted Core sdist suite: **379 passed, 1 existing Starlette warning**
- Extracted Ext sdist suite: **88 passed**
- Python 3.9 grammar parse: **25 Ext Python files passed**
- Raw YAML parse: passed
- `git diff --check`: passed
- CodeGraph: synchronized and up to date
- Isolated Core and Ext wheel/sdist builds: passed
- `twine check` for all four artifacts: passed
- Shared exact Core/Ext dependency and wheel-path verifier: passed
- Core-only clean venv and `pip check`: passed
- Core+Ext clean venv and `pip check`: passed
- Ext uninstall followed by Core import and `pip check`: passed
- Official ACP and MCP optional dependencies imported in the development venv
- Fake provider process residue after cancellation tests: none

The verified artifacts are local evidence only and are not release artifacts:

- `/private/tmp/unified-stage2-final2-core`
- `/private/tmp/unified-stage2-final2-ext`

No tag or package publication is authorized at this stage.

## Startup performance

Using the Stage 0 disposable-environment method with 30 fresh processes:

| Operation | Stage 0 median | Stage 2 median | Gate |
| --- | ---: | ---: | --- |
| `import unified_cli` | 48.606 ms | 42.508 ms | pass |
| `unified-cli --version` | 94.635 ms | 83.487 ms | pass |
| `import unified_cli_ext` | n/a | 35.719 ms | recorded |

Core startup remains within the allowed larger-of-50-ms-or-10-percent budget.
Neither Core startup operation discovers, imports, or probes an Ext provider.

## Mandatory boundary retained for provider adapters

Stage 2 does not authenticate a real provider. Stage 5 adapters must not inherit
the real user's HOME, Keychain, OAuth database, browser profile, or credential
files. Each adapter must define explicit credential brokering through the
provider's official authentication flow and a provider-specific minimal
environment. Real login, subscription entitlement, event schema, permission
meaning, session resume, and logout/revocation remain Preview until the isolated
Stage 6 smoke evidence exists.
