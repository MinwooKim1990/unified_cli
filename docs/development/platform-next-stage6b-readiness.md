# Platform Next Stage 6B local Docker readiness record

Stage 6B introduces the source-only `unified-ext-lab-real-docker` launcher for
an explicit local-engine synthetic conformance check. It complements, but does
not replace, the existing offline `unified-ext-lab` fake-Docker fixture.

## Boundary and evidence meaning

The Stage 6B tool uses only the repository's synthetic guest and fake-provider
fixture. It is not a Provider compatibility, installation, authentication,
account, entitlement, browser-callback, or paid-call validation. Its only path
arguments are the required canonical absolute state root and evidence output
used for private persistence. It accepts no caller-controlled Docker
executable, host, context, platform, provider, credential, URL, working
directory, argv, timeout, shell, guest command, or guest/runtime path input.

Its conformance operation never pulls. A separate `prepare-base` operation is
the only network-capable step and requires the explicit `--allow-network`
acknowledgement. Preparation is never implied by a conformance run.
Recovery uses a separate cleanup-only Docker discovery path. After checking
daemon reachability it reconstructs only the persisted resource plan and uses
the persisted immutable resource IDs and artifact evidence. It does not load
the forward routing hold, Buildx binary, base image, platform, or build
context, and it cannot resume forward work.

When an image or container ID is durable, cleanup inspects and removes that
exact daemon ID. Renames, tag changes, and label changes do not redirect or
block deletion of that exact object. A different object that later acquires
the planned name or labels is never substituted for the durable ID; it remains
visible to clean verification and keeps the run `DIRTY`. Before an ID is
durable, discovery remains conservative and requires the planned name and the
complete ownership-label set to agree before it records an ID or removes an
object.

Docker client failure does not prove that an already-submitted build or
container-create request cannot publish later in the daemon. The harness does
not use delay, polling history, or a client-process exit as a quiescence proof.
Any unresolved real-Docker mutation window therefore applies an irreversible
promotion taint. Cleanup and later cleanup retries remain available, including
for a late-published object, but clean verification remains `DIRTY` and
evidence sealing is permanently refused for that run.

All resulting manifests are permanently marked `promotion_eligible=false`.
They cannot promote an Ext Provider from `Held`, authorize a tag or package
publication, or alter `main` or any release branch.

## Packaging boundary

Both lab launchers and the complete `tools/unified_ext_lab/` tree are
source-checkout-only. Core package discovery is limited to `src/`; Ext package
discovery is limited to `packages/unified-cli-ext/src/`. The Core sdist manifest
prunes `tools/` and excludes every `scripts/unified-ext-lab*` launcher. CI
inspects the actual built sdist for the same complete prefix gate, and
distribution tests assert that exact boundary.

## Execution status

No actual Docker invocation has occurred for Stage 6B. This readiness record
therefore documents the reviewed synthetic conformance boundary and packaging
exclusions, not successful Docker execution. Any future local-engine run must
be separately authorized and retain its non-promotional evidence status.
