# unified-ext-lab development harness

`unified-ext-lab` is a source-only development harness for validating the
`unified-cli-ext` execution boundary. It is not part of either PyPI package and
does not change the installed `unified-cli` command.

## Current scope

The first Stage 6 slice is deliberately offline and fixture-only:

- it constructs a finite set of direct Docker argv tuples;
- tests execute those tuples through an in-memory Docker model;
- the image and provider fixtures are synthetic and pinned by SHA-256;
- the locked base image uses the non-routable `registry.invalid` namespace;
- no provider CLI is installed, authenticated, or contacted;
- its evidence is permanently marked `harness_fixture`, `fake_docker`, and
  `promotion_eligible=false`.

Passing this scaffold proves the command, ownership, lifecycle, cleanup, and
recording contracts. It does not promote any Ext provider from `Held` and is
not evidence of real Docker or provider compatibility.

## Repository boundary

All harness code lives under `tools/unified_ext_lab/` with a source checkout
launcher under `scripts/`. Core only discovers packages below `src/`, while Ext
only discovers packages below `packages/unified-cli-ext/src/`. CI additionally
checks that neither wheel nor sdist contains the harness.

The harness must never be installed editable into the maintainer's active
`unified-cli` environment. Development and tests run from the dedicated
`codex/platform-ext-lab` worktree with an explicit `PYTHONPATH`.

## Fixed execution boundary

The Docker command builder has no shell-string, Compose, prune, wildcard
deletion, host-network, bind-mount, or arbitrary guest-command surface. The
container specification requires:

- UID/GID `65532:65532`;
- a read-only root filesystem;
- all capabilities dropped and `no-new-privileges` enabled;
- no network;
- bounded CPU, memory, process count, open files, and output;
- a fixed `tmpfs` for `/tmp`;
- separate named workspace, auth, and tool volumes;
- no host HOME, Keychain, SSH agent, Docker socket, git configuration, or
  credential-helper mount;
- a private Docker CLI HOME, config directory, and temporary directory.

Every managed object has a deterministic name plus the complete lab,
provider, role, and random ownership-token label set. Cleanup checks both the
exact recorded name and the complete label set, then validates inspect data
before acting. A renamed, relabeled, duplicated, or policy-drifted object is
reported as remaining and is not removed.

## Durable lifecycle

Each side effect is preceded by a durable `*_PENDING` state. Loading a non-seal
pending state after interruption appends exactly one failed `interrupted`
observation and converts it to `RECOVERY_REQUIRED`; forward create, install,
test, or evidence work cannot resume from recovery. Recovery can only perform
local logout, exact-object removal, clean verification, and evidence sealing.
`SEAL_PENDING` is deliberately different: it retains a non-sensitive hash of
the canonical output path, the exact expected payload hash, and the result.
After a crash it publishes an absent output or accepts an existing output only
when it is an owned, regular `0600` file containing the exact canonical bytes.
The normal accepted form has one link. If publication completed immediately
before generated-temporary cleanup, reconciliation removes only an exactly
named generated temporary that has the same file identity, owner, mode, and
expected bytes and that fully explains the link count. It then syncs the
directory and requires one remaining link. Any unrelated name, identity,
content, permission, or unexplained link is refused without overwrite.

The intended sequence is:

```text
NEW -> CREATE_PENDING -> CREATED
    -> INSTALL_PENDING -> INSTALLED
    -> TEST_PENDING -> TESTED
    -> EVIDENCE_PENDING -> EVIDENCE_CAPTURED
    -> LOGOUT_PENDING -> LOGOUT_DONE | LOGOUT_FAILED
    -> DESTROY_PENDING -> DESTROY_DONE | DESTROY_FAILED
    -> VERIFY_CLEAN_PENDING -> CLEAN_VERIFIED | DIRTY
    -> SEAL_PENDING -> PASSED | FAILED_CLEAN
```

State directories are mode `0700`; state, lock, and final evidence files are
mode `0600`. Writes are canonical JSON, bounded, duplicate-key rejecting,
atomically replaced, and directory-synced. Final evidence is create-only and
cannot overwrite an unrelated existing file. New state directories sync the
child before the parent directory entry. Cleanup records each successfully
removed role immediately in an append-only state ledger; manifest created,
removed, and remaining counts come only from that ledger and must agree with
the final exact-resource inspection.

The per-lab process lock pins the private lab directory and opens `state.lock`
relative to that directory without following links. It validates the opened
file and the current directory entry before and after locking, so replacing
the lock filename cannot create two active harness contexts. This is a
cooperation boundary among harness processes, not a claim that an unrelated
same-user process that ignores advisory locking can be controlled.

An interactive shell, when implemented in the later real-Docker slice, must
durably taint the lab before it starts. Tainted runs can be cleaned but can
never capture or seal evidence.

## Evidence meaning

The persisted allowlist contains only immutable artifact identity, schema
hashes, bounded operation outcomes and latencies, cleanup counts, and the lab
and provider IDs. It excludes argv, stdout, stderr, prompts, responses,
environment values, account/session details, host paths, process IDs, receipts,
and credential material.

The manifest schema hash and observed protocol schema hash are independent.
Neither is derived from prompts or command output. A clean result requires zero
remaining owned resources; any earlier operation, local logout, or removal
failure is reported as `failed_clean`, even if later cleanup succeeds.
`captured_at_ns` is the non-negative Unix wall-clock timestamp in nanoseconds
at which the immutable evidence draft was captured. It is not a duration or a
provider timestamp; operation durations use the separate monotonic
`latency_ns` fields. Recovery never rewrites the captured value.

## Local checks

The source-checkout launcher runs the complete in-memory fixture lifecycle and
creates a non-promotional manifest. Both paths must be absolute and canonical;
their existing parents must be owned by the current user with mode `0700`.

```sh
scripts/unified-ext-lab fixture-run \
  --lab-id fixture-one \
  --state-root /absolute/private/state \
  --evidence-output /absolute/private/result.json \
  --json

scripts/unified-ext-lab status \
  --lab-id fixture-one \
  --state-root /absolute/private/state \
  --json

scripts/unified-ext-lab fixture-recover \
  --lab-id fixture-one \
  --state-root /absolute/private/state \
  --evidence-output /absolute/private/result.json \
  --json
```

`fixture-run` always uses the in-memory runner. It accepts no executable,
provider, URL, account, credential, or shell input. The separate create,
install, shell, test, and real-engine commands remain unavailable until the
later opt-in conformance gate is implemented and independently reviewed.
`fixture-recover` is also offline-only. It reconstructs the same synthetic
specification and in-memory runner from the private state identity and token,
performs no forward create/install/test/evidence work, and starts at the
earliest safe logout, destroy, verify, or seal step implied by state. A
`SEAL_PENDING` run performs seal reconciliation only. `fixture-run` converts
unexpected in-process interruption into durable recovery and attempts cleanup;
keyboard interruption returns exit status `130`. A `SIGKILL` cannot run a
handler, so a later explicit `fixture-recover` invocation is required.

The fixture suite does not require Docker or network access:

```sh
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=.:src:packages/unified-cli-ext/src \
python -m pytest -q tools/unified_ext_lab/tests
```

Before this slice can merge into `codex/platform-next`, it must also pass:

- Python 3.9 grammar checks;
- the full Core and Ext offline suites;
- clean Core and Ext distribution builds with no harness files;
- an independent lifecycle/cleanup review;
- an independent runner/command-boundary review;
- zero P0, P1, or unresolved P2 findings.

Real Docker, provider installation, provider login, browser callbacks, and paid
calls belong to later opt-in gates and are not started by this scaffold.
