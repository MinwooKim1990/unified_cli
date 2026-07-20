# Platform-next Stage 1 audit

Recorded on 2026-07-20 from the `codex/platform-core-abi` feature branch,
based on Stage 0 commit `d33b6878f9600811af2a7a794900c93631a719a4`.

## Scope

Stage 1 adds the Core-owned `unified_cli.providers.v1` plugin ABI and lazy
registry without adding a provider implementation or a required dependency.
The public `PROVIDERS` mapping remains exactly `claude`, `codex`, and `gemini`;
`agy` remains a reserved executable alias. Core create, routing, help, version,
and default provider listing do not enumerate entry points.

Extension discovery is metadata-only until a caller explicitly selects an
extension ID. The registry rejects reserved or Core-inference-compatible IDs,
duplicates, invalid ABI and metadata, re-entrant loads, and cross-thread nested
load cycles. `UNIFIED_CLI_DISABLE_PLUGINS=1` disables discovery and loading.
The existing HTTP server remains Core-only and does not treat a plugin-declared
server policy as authorization.

## Independent Sol audit

The first independent review found loader deadlocks, Core slash-route
regressions, hostile metadata handling, unbounded model metadata, server route
policy changes, and extension exception-boundary gaps. Those findings were
fixed and converted into regression tests.

The final review then found three P1 issues at the runtime proxy boundary:

1. a plugin-created `UnifiedError` could expose plugin-owned message or cause
   text;
2. proxy construction read plugin attributes outside the factory error
   boundary; and
3. closing the Core async stream proxy did not immediately close the inner
   async iterator.

All three were fixed and covered by sync/async, traceback redaction, factory
attribute failure, cancellation, and early-close tests. The same independent
reviewer re-audited the final diff with this result:

- P0: **0**
- P1: **0**
- P2: **0**
- Gate: **PASS**

## Verification

- Full suite: **379 passed, 1 existing Starlette deprecation warning**
- ABI and server hardening focus: **72 passed**
- Python 3.9 grammar parse: **25 source files passed**
- `git diff --check`: passed
- CodeGraph: synchronized and up to date
- Isolated wheel and sdist build: passed
- `twine check` for both artifacts: passed

The isolated build artifacts still report the development base version 0.4.0.
The coordinated 0.5.0 version change is intentionally deferred until the Core
and Ext packaging matrix is assembled; no artifact from this audit is for
publication.

## Startup performance

Using the Stage 0 allowlisted-environment method with 30 fresh processes:

| Operation | Stage 0 median | Stage 1 median | Gate |
| --- | ---: | ---: | --- |
| `import unified_cli` | 48.606 ms | 42.609 ms | pass |
| `unified-cli --version` | 94.635 ms | 79.282 ms | pass |

Both results are below the Stage 0 medians and therefore within the allowed
regression budget of the larger of 50 ms or 10 percent. Neither command loads
or probes an extension provider.

## Trust boundary retained for Stage 2

An explicitly selected entry-point plugin executes Python in the caller's
process and must be treated as trusted installed code. Core isolates discovery,
validation, public runtime errors, and server exposure; process, transport,
environment allowlisting, permissions, and adapter-specific protocol handling
belong to `unified-cli-ext` and are the next stage's audit scope.
