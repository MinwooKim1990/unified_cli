# unified-ext-lab development harnesses

The repository has two source-only development harnesses for the
`unified-cli-ext` execution boundary. Neither is part of the Core or Ext wheel
or sdist, and neither changes the installed `unified-cli` command.

- `unified-ext-lab` is the existing offline, in-memory fake-Docker fixture.
- `unified-ext-lab-real-docker` is a separate local-engine synthetic
  conformance tool. It is not Provider compatibility, authentication, account,
  or entitlement validation.

Every evidence manifest emitted by either tool is marked
`promotion_eligible=false`; dirty or held runs may emit no evidence manifest.
Any emitted manifest is useful only as bounded engineering evidence; it does
not change a Provider from `Held`, does not authorize a release, and does not
affect `main` or release branches.

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

## Stage 6B local Docker conformance scope

`unified-ext-lab-real-docker` is an explicit opt-in tool for a maintainer's
already-running local Docker engine. It runs only the repository's synthetic
guest and fake provider fixture. It accepts canonical absolute paths only for
the required `--state-root` and `--evidence-output` persistence outputs. It
accepts no caller-controlled Docker executable, host, context, platform,
provider, account, credential, URL, working directory, argv, timeout, shell,
guest command, or guest/runtime path input. In particular, it cannot be
redirected to a provider CLI, another Docker endpoint, or arbitrary host files
through generic command-line parameters.

The conformance run never pulls or builds an image and does not discover or
invoke Buildx. Base-image preparation is a distinct operation and requires the
explicit `--allow-network` acknowledgement; it is the only operation that may
contact a registry. A run re-inspects the immutable named digest, requires it
to resolve to the locked local image ID and platform, and creates the container
from that exact ID with `--pull=never`. Running preparation is not implicit in
a conformance invocation.

No actual Docker invocation has been made for Stage 6B at the time of this
record. The implementation and its tests establish a local-engine synthetic
conformance boundary only. Docker availability, real provider installation,
authentication, browser callbacks, account access, paid calls, and provider
protocol compatibility remain outside this gate and require separate approval
and provider-specific evidence.

## Repository boundary

All harness code lives under `tools/unified_ext_lab/` with source-checkout
launchers under `scripts/`. Core only discovers packages below `src/`, while
Ext only discovers packages below `packages/unified-cli-ext/src/`. The Core
sdist manifest prunes the complete `tools/` tree and excludes every launcher
matching `scripts/unified-ext-lab*`. CI inspects the built archives and checks
that no lab code or matching launcher enters either wheel or sdist.

The harness must never be installed editable into the maintainer's active
`unified-cli` environment. Development and tests run from dedicated platform
worktrees with an explicit `PYTHONPATH`.

## Fixed execution boundary

The Docker command builder has no shell-string, Compose, prune, wildcard
deletion, host-network, caller-controlled bind-mount, or arbitrary
guest-command surface. The container specification requires:

- UID/GID `65532:65532`;
- a read-only root filesystem;
- all capabilities dropped and `no-new-privileges` enabled;
- no network;
- bounded CPU, memory, process count, open files, and output;
- a fixed `tmpfs` for `/tmp`;
- no host HOME, Keychain, SSH agent, Docker socket, git configuration, or
  credential-helper mount;
- a private Docker CLI HOME, config directory, and temporary directory.

Stage 6A's offline fixture model uses separate named workspace, auth, and tool
volumes. Its cleanup recognizes a fixture object only when both its planned
name and its complete lab, provider, role, and random ownership-token label
set agree, then validates the inspected fixture data before acting. A renamed,
relabeled, duplicated, or policy-drifted fixture object is left in place and
reported as remaining.

Stage 6B's real-Docker run instead requires ephemeral storage: `/tmp` and the
workspace, auth, and tool locations use fixed `tmpfs` mounts, all with
`noexec`, so it does not create named volumes. The locked Python tool copied to
the writable tool mount is invoked through the immutable base interpreter.
The only new managed daemon resource is the container; the locked base image
is inspected but never managed or removed. Once the daemon has returned a
container ID and the harness has made it durable, cleanup targets that exact
ID. Later name or label drift does not redirect that deletion. Before the ID
is durable, discovery is intentionally conservative: the planned name and the
complete ownership-label set must agree before an object can be recorded or
removed. A replacement object is never substituted for the durable ID; it
remains visible to clean verification and keeps the run `DIRTY`. Cleanup also
accepts the exact historical five-role plan so interrupted older runs can
remove their managed container and image without treating the base as owned.

Before any forward container action, the run creates an exact `NEW` intent and
then copies the hash-locked context into
`real-docker-v1/<lab-id>/runtime-snapshot`. After snapshot and spec validation,
it atomically binds the fixture artifact to that intent. Docker receives only
the fixed read-only bind from the snapshot's guest directory to
`/opt/unified-ext-lab`; the source path is derived from private state and is
not caller-selectable. The lifecycle removes this snapshot with no-follow,
descriptor-relative traversal only after container cleanup succeeds, and
clean verification requires it to be absent. A kill before or after artifact
binding therefore leaves a deterministic resource that
`conformance-recover` can find and remove.

## Durable lifecycle

Each side effect is preceded by a durable `*_PENDING` state. Loading a non-seal
pending state after interruption appends exactly one failed `interrupted`
observation and converts it to `RECOVERY_REQUIRED`; forward create, install,
test, or evidence work cannot resume from recovery. Recovery can only perform
local logout, exact-object removal, clean verification, and evidence sealing.
If interruption occurs between operations while state is stably `NEW`,
`CREATED`, `INSTALLED`, or `TESTED`, the run handler and explicit recovery
command durably convert that phase to the corresponding cleanup-only
`RECOVERY_REQUIRED` state before cleanup. They never re-enter the next forward
pending phase.
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

Neither launcher exposes an interactive shell. More generally, interactive
shell use or unresolved uncertainty about whether a daemon mutation occurred
creates a permanent evidence and promotion hold: cleanup may continue, but
evidence capture, sealing, and any promotion use remain held for that run.
This is an evidence safeguard, not a claim that cleanup cannot still succeed.

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
provider, URL, account, credential, or shell input.
`fixture-recover` is also offline-only. It reconstructs the same synthetic
specification and in-memory runner from the private state identity and token,
performs no forward create/install/test/evidence work, and starts at the
earliest safe logout, destroy, verify, or seal step implied by state. A
`SEAL_PENDING` run performs seal reconciliation only. `fixture-run` converts
unexpected in-process interruption into durable recovery and attempts cleanup;
keyboard interruption returns exit status `130`. A `SIGKILL` cannot run a
handler, so a later explicit `fixture-recover` invocation is required.

The separate Stage 6B launcher exposes only its fixed command grammar. The
state root is a caller-owned private canonical directory; real-Docker state is
always placed in its internal `real-docker-v1/<lab-id>` namespace and that
namespace cannot be selected from the command line.

```sh
scripts/unified-ext-lab-real-docker prepare-base \
  --allow-network \
  --json

scripts/unified-ext-lab-real-docker conformance-run \
  --lab-id conformance-one \
  --state-root /absolute/private/state \
  --evidence-output /absolute/private/result.json \
  --json

scripts/unified-ext-lab-real-docker conformance-status \
  --lab-id conformance-one \
  --state-root /absolute/private/state \
  --json

scripts/unified-ext-lab-real-docker conformance-recover \
  --lab-id conformance-one \
  --state-root /absolute/private/state \
  --evidence-output /absolute/private/result.json \
  --json
```

`prepare-base` is the only network-capable command and does nothing without
the exact `--allow-network` flag. `conformance-run` requires a reachable fixed
Docker engine and an already-local locked base image; it neither builds nor
loads Buildx. `conformance-recover` performs daemon reachability checking
followed only by state stabilization, logout, exact removal, derived-snapshot
cleanup, clean verification, and sealing; it never pulls or resumes create,
install, test, or evidence work. Its cleanup-only discovery does not load or
enforce the forward routing hold, Buildx binary, base-image lock, platform, or
source build context. It derives only the fixed snapshot resource path from
the validated private state namespace and lab ID. `conformance-status`
performs no Docker discovery or daemon probe.

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

Actual Stage 6B Docker execution is a separately approved, opt-in operational
gate and is not started by the fixture scaffold. Provider installation, login,
browser callbacks, and paid calls are later provider-specific opt-in gates.
The source-only Stage 6B real-Docker launcher remains synthetic and
non-promotional.
