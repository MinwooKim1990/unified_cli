# Platform Next Stage 5D compatibility record

Stage 5D adds the third Ext catalog cohort. The work was completed on
`codex/platform-ext-cohort-03`; `main` was not modified.

## Included

- Held catalog entries for OpenCode and Kilo Code
- an explicit required-sentinel declaration for the OpenCode one-shot
  candidate, with the previous Held-helper default preserved
- candidate JSONL metadata for OpenCode and ACP metadata for Kilo Code
- expanded generated EN/KO support tables and Ext package documentation
- regression coverage for exactly 11 inert catalog entries

Both entries remain Held. They advertise no executable Core capability, are
unavailable to the Core HTTP server, and stop before provider callbacks. No
OpenCode or Kilo binary was installed, signed in, or called in this stage.

## Independent Sol review

Three independent reviews covered metadata and lifecycle behavior, packaging
and Core isolation, and EN/KO documentation. The first two reviews passed
without findings. The documentation review identified one moderate wording
item: the public Kilo row claimed a candidate update-control value before its
effect had been verified. The row now makes no update-control claim and leaves
that behavior for Stage 6 evidence.

Final result after the correction and focused recheck:

- release blockers: 0
- high findings: 0
- moderate findings: 0

## Verification

- Core: 603 passed; one known Starlette/httpx transition warning
- Ext: 309 passed, 1 intentional platform-dependent skip
- focused Held-provider and generated-document suite: 48 passed
- independent focused Held/plugin ABI suite: 102 passed
- Python 3.9 grammar: passed for all 66 packaged Python modules
- generated support-table check: passed
- wheel and sdist description and record checks: passed for all four artifacts
- wheel and sdist Python sources: byte-for-byte aligned
- Core/Ext wheel paths: no overlap
- clean Core+Ext install with the optional ACP extra and `pip check`: passed
- passive discovery exposed exactly 11 Ext IDs without importing provider
  modules
- explicit OpenCode and Kilo selection returned the Core-owned Held response
  without starting a provider process
- Ext removal left Core 0.5.0 and its three built-ins healthy

The independent package review measured entry-point list processing at a
median 26.380 microseconds for nine entries and 27.823 microseconds for eleven,
an observed increase of about 1.443 microseconds. The default Core execution
path did not import Ext or gain measurable work.

## Gate decision

Stage 5D passes its compatibility gate and may be merged into
`codex/platform-next`. It does not authorize a merge to `main`, a package
upload, a tag, or a release. Provider enablement remains conditional on later
isolated installation and provider-specific verification.
