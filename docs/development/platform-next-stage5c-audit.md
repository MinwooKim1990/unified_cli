# Platform Next Stage 5C compatibility record

Stage 5C adds the second Ext catalog cohort. The work was completed on
`codex/platform-ext-cohort-02`; `main` was not modified.

## Included

- Held catalog entries for CodeBuddy, Qoder, Mistral Vibe, Qwen Code, and
  Cline
- exact plugin entry-point metadata for all five providers
- provider-specific candidate binary, transport, prompt, environment, and
  documentation metadata
- expanded generated EN/KO support tables and Ext package documentation
- regression coverage for all nine Held catalog entries, including the
  hyphenated `mistral-vibe` module target

All five new entries remain Held. They advertise no executable Core
capability, are unavailable to the Core HTTP server, and stop before provider
callbacks. No vendor CLI was installed, signed in, or called in this stage.

## Independent Sol review

Three independent reviews covered provider metadata and lifecycle behavior,
packaging and Core regression isolation, and EN/KO documentation. The first
two reviews passed without findings. The documentation review identified
three moderate items, all repaired before the gate was rerun:

1. the CodeBuddy ACP link now points to the current official page;
2. Cline is described as having separate JSONL and ACP candidates rather than
   implying that its ACP surface does not yet exist;
3. the Ext README now states that this inactive catalog release does not take
   part in vendor sign-in or request handling.

Final result after the corrections and focused recheck:

- release blockers: 0
- high findings: 0
- moderate findings: 0

## Verification

- Core: 603 passed; one known Starlette/httpx transition warning
- Ext: 301 passed, 1 intentional platform-dependent skip
- focused Stage 5C Held-provider suite: 40 passed
- independent focused ABI, Held-provider, generated-doc, Python 3.9, package,
  and distribution suite: 112 passed
- Python 3.9 source grammar: passed for all 64 source files
- generated support-table check: passed
- wheel and sdist metadata checks: passed for all four artifacts
- Core/Ext wheel paths: no overlap
- clean Core+Ext install with the optional ACP extra and `pip check`: passed
- passive discovery exposed exactly nine Ext IDs without importing provider
  modules
- explicit selection of every new provider returned the Core-owned Held
  response without starting a vendor process
- Ext removal left Core 0.5.0 and its three built-ins healthy

The five new modules add 4,702 compressed bytes relative to the Stage 5B Ext
wheel. Core dependencies and the default Core import path are unchanged.

## Gate decision

Stage 5C passes its compatibility gate and may be merged into
`codex/platform-next`. It does not authorize a merge to `main`, a package
upload, a tag, or a release. Provider enablement remains conditional on later
isolated installation and provider-specific verification.
