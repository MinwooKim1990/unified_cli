# Platform Next Stage 5F compatibility record

Stage 5F completes the initial Ext catalog with Amp CLI and GitLab Duo CLI.
The work was completed on `codex/platform-ext-cohort-05`; `main` was not
modified.

## Included

- inert Held catalog metadata for Amp CLI and GitLab Duo CLI
- explicit candidate metadata for Amp streaming JSONL and GitLab Duo one-shot
  JSON execution
- exhaustive Stage 6 evidence markers for identity, installation, protocol,
  authentication, sessions, permissions, configuration, cancellation, update,
  and removal behavior
- generated EN/KO support tables and package metadata for exactly 18 Ext
  catalog entries

Both entries remain Held. They expose no Core capability, remain disabled for
server use, and stop before provider callbacks, executable lookup, or process
start when selected. No vendor CLI was installed, authenticated, or called in
this stage.

## Independent Sol review

Two independent reviews covered current vendor documentation, candidate
metadata, lifecycle behavior, Core isolation, packaging, documentation, and
passive-discovery performance.

The documentation review found that an initial draft described the official
GitLab Duo npm package as deprecated. Current GitLab documentation and the
9.6.0 package still present `@gitlab/duo-cli` as an active installation path.
The EN/KO table, module note, and evidence-marker name were corrected to cover
both the compiled generic package and official npm package without claiming
deprecation. The same review found one stale Stage 5B-5E test-module label; it
was updated to Stage 5B-5F.

The final metadata review confirmed the Amp package transition to
`@ampcode/cli`, its approval and settings caveats, and its stdin/process
lifecycle gates. It also confirmed GitLab Duo 9.6.0 headless JSON behavior,
bare-semver identity limitation, automatic tool approval, configuration
sources, and Windows-specific transport gate. These behaviors remain recorded
as later verification requirements rather than executable claims.

Final result:

- release blockers: 0
- high findings: 0
- moderate findings: 0

## Verification

- Core: 603 passed; one known Starlette/httpx transition warning
- Ext: 337 passed, 1 intentional platform-dependent skip
- independent focused suites: 156 passed and 132 passed, 2 skips
- local focused metadata/document/package suite: 86 passed
- Python 3.9 grammar: passed for all 73 packaged Python modules
- generated support-table check: passed
- wheel and sdist description checks: passed for all four artifacts
- Ext wheel and sdist sources: aligned with the final package source snapshot
- Ext wheel RECORD: valid with exactly 18 unique provider entry points
- Core/Ext wheel paths: no overlap
- clean Core+Ext installation with the optional ACP extra and `pip check`:
  passed
- passive discovery exposed exactly 18 Ext IDs without importing provider
  modules or starting provider work
- explicit selection of Amp or GitLab Duo returned the Core-owned Held response
  before executable discovery
- Ext removal left Core 0.5.0 and the `claude`, `codex`, and `gemini` built-ins
  healthy

The independent package review measured cold Core import at a 107.193 ms
median, `--version` at 121.390 ms, and cold passive discovery at 94.789 ms over
30 runs each. Isolated registry processing growth from 16 to 18 entries was
2.334 microseconds per call over 2,500 runs. Default Core import, `--version`,
built-in listing, and passive Ext discovery started no provider process or
network probe.

## Gate decision

Stage 5F passes its compatibility gate and may be merged into
`codex/platform-next`. It does not authorize a merge to `main`, a package
upload, a tag, or a release. All 18 extension providers remain Held until the
separate Stage 6 lab and provider-specific evidence gates are complete.
