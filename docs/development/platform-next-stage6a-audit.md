# Platform Next Stage 6A offline lab record

Stage 6A adds the source-only `unified-ext-lab` foundation used to verify the
later real-provider test environment. Work was completed on
`codex/platform-ext-lab`; `main` and `codex/platform-next` were not modified.

This is an offline fixture gate, not real provider certification. It does not
start Docker, install a provider CLI, perform login, use an account, or make a
paid call. All 18 Ext providers remain Held until their separate opt-in
verification gates are completed.

## Included

- an immutable synthetic artifact and in-memory Docker model
- exact, shell-free command construction for a future private Docker lab
- private state, operation ledger, restart recovery, and canonical evidence
- exact managed-resource identity and cleanup accounting
- a bounded, identity-bound subprocess runner with process-group cleanup
- a source-checkout launcher that ignores Python import environment variables
  and refuses a symbolic-link entrypoint
- `fixture-run`, `fixture-recover`, `status`, and `describe` commands only
- Linux Python 3.9 and macOS Python 3.14 CI coverage
- Core/Ext wheel and sdist boundary checks

The public fixture command accepts no executable, image, URL, account,
credential, provider command, arbitrary working directory, or shell input.
Evidence is permanently marked as fixture-only and cannot promote a Provider.

## Consistency and cleanup review

Independent Sol reviews covered command construction, subprocess lifecycle,
state transitions, restart behavior, evidence publication, CLI exit mapping,
packaging, and Python 3.9/current compatibility.

Review rounds found and corrected:

- process finalization paths that could previously lose cleanup certainty;
- lock filename replacement that could allow two harness contexts to proceed;
- a publication interruption point that could block a later seal retry;
- incomplete Core/Ext wheel member and metadata-directory validation;
- non-finite or impractically large runner timeouts;
- constructor and close paths that could leave a private runner directory after
  an interrupted setup or failed first removal.

The state lock now pins and locks the private lab directory, opens the lock file
relative to that directory without following links, validates file identity
before and after locking, and refuses an identity mismatch. Evidence retry
removes only a fully explained generated temporary name with the expected
identity, owner, mode, bytes, and link count, syncs the directory, and then
requires one final link. Any unexplained file remains untouched and causes a
refusal.

Final independent result:

- P0: 0
- P1: 0
- P2: 0

## Verification

- Core full suite: 611 passed, with one known Starlette/httpx transition warning
- Ext full suite: 337 passed, 1 intentional platform-dependent skip
- offline lab suite on current Python 3.14: 132 passed
- offline lab suite on macOS system Python 3.9: 132 passed
- focused runner suite on both interpreters: 21 passed each
- focused state suite on both interpreters: 29 passed each
- focused evidence suite on both interpreters: 23 passed each
- distribution verifier suite: 20 passed
- fresh Core and Ext wheel/sdist builds: passed
- `twine check` for all four artifacts: passed
- exact Core/Ext wheel member separation: passed
- Core and Ext sdists contain no source-only lab files: passed
- clean Core-only and Core+Ext temporary environments: `pip check` passed
- Ext installed entry points: exactly 18
- Ext removal left Core 0.5.0 working with zero Ext entry points
- CI workflow YAML parse and `git diff --check`: passed
- CodeGraph index: current

## Gate decision

The Stage 6A offline lab foundation passes its gate and may be merged into
`codex/platform-next`. This does not authorize a merge to `main`, a tag, a
package upload, or a GitHub Release.

The remaining Stage 6 work is opt-in real-environment validation: start the
dedicated Docker lab, install exact representative CLI versions, perform only
approved authentication, collect non-sensitive compatibility evidence, log
out, revoke test grants when applicable, and verify complete lab cleanup.
Those actions require a separate user approval and are not performed by this
commit.
