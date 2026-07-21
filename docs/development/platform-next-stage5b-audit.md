# Platform Next Stage 5B compatibility record

Stage 5B adds the first Ext catalog cohort and the common execution evidence
needed before any provider can be enabled. The work was completed on
`codex/platform-ext-cohort-01`; `main` was not modified.

## Included

- Held catalog entries for Grok, Kimi, Copilot, and Cursor
- separate registry lifecycle and integration support status
- local installation receipts for direct executables and scoped npm launchers
- an ACP 0.11 text-turn runtime with ordered event normalization
- metadata-generated EN/KO provider support tables with a CI consistency check
- Core/Ext documentation describing lazy discovery and the local-file receipt
  boundary

All four catalog entries remain Held. They advertise no executable Core
capability, are not available to the Core HTTP server, and stop before provider
callbacks. No vendor CLI was installed, signed in, or called in this stage.

## Independent Sol review

The final review found two items, both repaired before the gate was rerun:

1. omitted plugin support status now defaults to `experimental`, while Core
   built-ins remain explicitly `stable` and this cohort remains explicitly
   `held`;
2. exceptions raised while Core reads extension metadata are replaced with a
   fresh Core-owned metadata error after leaving the metadata evaluation
   boundary.

Final result:

- release blockers: 0
- high findings: 0
- moderate findings: 0

## Verification

- Core: 603 passed; one known Starlette/httpx transition warning
- Ext: 281 passed, 1 intentional platform-dependent skip
- ACP 0.11 focused suite: 25 passed
- focused ABI, ACP, Held, receipt, generated-doc, and distribution checks:
  135 passed in the independent review
- Python 3.9 source grammar and Core+Ext imports: passed
- generated support-table check: passed
- wheel and sdist metadata checks: passed for all four artifacts
- Core/Ext wheel paths: no overlap
- clean Core+Ext install and `pip check`: passed
- Ext removal left Core and its three built-ins healthy
- ACP optional extra remained lazy until explicitly imported

Interleaved startup measurements showed no Stage 5B regression relative to
the integration base. The independent review measured Core import at 127.5 ms
versus 178.3 ms and `--version` at 181.1 ms versus 181.6 ms on the same host.

## Gate decision

Stage 5B passes its compatibility gate and may be merged into
`codex/platform-next`. It does not authorize a merge to `main`, a package
upload, a tag, or a release. Provider enablement remains conditional on the
later isolated installation and provider-specific verification stages.
