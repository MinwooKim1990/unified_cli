# Platform Next Stage 5E compatibility record

Stage 5E adds the fourth Ext catalog cohort. The work was completed on
`codex/platform-ext-cohort-04`; `main` was not modified.

## Included

- inert Held catalog entries for Factory Droid, Pi Coding Agent, Oh My Pi,
  Hermes Agent, and Poolside Agent CLI
- explicit candidate metadata for vendor stream JSON-RPC, custom NDJSON RPC,
  and ACP stdio transports
- exhaustive Stage 6 evidence-marker coverage for each new entry
- expanded generated EN/KO support tables and package documentation for
  exactly 16 Ext catalog entries

Every new entry remains Held. Core sees no executable capability, the Core
server keeps extension providers disabled, and an explicit selection stops
before a provider callback, executable lookup, or process start. No vendor CLI
was installed, authenticated, or called in this stage.

## Independent Sol review

Independent reviews covered metadata and lifecycle behavior, packaging and
Core isolation, bounded passive-discovery performance, and EN/KO documentation.

The first metadata review found three moderate evidence-record omissions:

- the Hermes ACP 0.9.0 versus Ext ACP 0.11.x compatibility gate was not listed
  in the exhaustive test inventory;
- Pi's candidate `--offline` boundary did not separately record update,
  package, and telemetry verification; and
- Poolside did not separately record install-channel and binary-identity
  verification.

All three markers and tests were added. The evidence test now compares the
complete set of recorded Stage 6 markers with its expected inventory. A second
Sol review passed the corrected metadata with no remaining finding.

The documentation review briefly reported two current-version mismatches from
stale cached pages. Fresh primary registry responses confirmed `droid` 0.176.0
and `@factory/cli` 0.176.0 at the same source commit, and the Poolside release
API confirmed v1.0.13 with checksummed platform assets. The reviewer withdrew
both findings and passed the current EN/KO documentation.

Final result:

- release blockers: 0
- high findings: 0
- moderate findings: 0

## Verification

- Core: 603 passed; one known Starlette/httpx transition warning
- Ext: 329 passed, 1 intentional platform-dependent skip
- focused Held-provider and generated-document suite: 68 passed
- Python 3.9 grammar: passed for all 71 packaged Python modules
- generated support-table check: passed
- wheel and sdist description checks: passed for all four artifacts
- Ext wheel and sdist sources: byte-for-byte aligned with current package
  sources
- Core/Ext wheel paths: no overlap
- clean Core+Ext installation with the optional ACP extra and `pip check`:
  passed
- passive discovery exposed exactly 16 Ext IDs without importing provider
  modules or starting provider work
- explicit selection of all five Stage 5E IDs returned the Core-owned Held
  response
- Ext removal left Core 0.5.0 and its three built-ins healthy

The independent package review measured all 16 entry points at 3.878 ms on a
cold passive enumeration. Isolated processing growth from 11 to 16 entries was
6.582 microseconds per call. Default Core import, `--version`, built-in listing,
and dashboard metadata imported no Ext provider module and started no provider
work.

## Gate decision

Stage 5E passes its compatibility gate and may be merged into
`codex/platform-next`. It does not authorize a merge to `main`, a package
upload, a tag, or a release. Provider enablement remains conditional on later
isolated installation and provider-specific verification.
