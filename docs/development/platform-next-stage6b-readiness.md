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

Its conformance operation never pulls or builds and does not discover or load
Buildx. A separate `prepare-base` operation is the only network-capable step
and requires the explicit `--allow-network` acknowledgement. Preparation is
never implied by a conformance run. Forward preflight inspects the immutable
named digest and requires the locked local image ID and platform; container
creation then uses that exact ID with `--pull=never`.
Recovery uses a separate cleanup-only Docker discovery path. After checking
daemon reachability it reconstructs only the persisted resource plan and uses
the persisted immutable resource IDs and artifact evidence. It derives the
fixed runtime-snapshot path from the private state namespace and validated lab
ID, but it does not load the forward routing hold, Buildx binary, base image,
platform, or source build context, and it cannot resume forward work.

New runs manage only the container; the locked base image is never owned or
removed. When the container ID is durable, cleanup inspects and removes that
exact daemon ID. Renames and label changes do not redirect or block deletion
of that exact object. A different object that later acquires the planned name
or labels is never substituted for the durable ID; it remains visible to
clean verification and keeps the run `DIRTY`. Before an ID is durable,
discovery remains conservative and requires the planned name and the complete
ownership-label set to agree before it records an ID or removes an object.
The cleanup grammar retains exact compatibility for the historical five-role
plan, including its managed per-run image, without ever treating the locked
base image as owned.

The exact `NEW` intent is durable before the hash-locked context is copied to
`real-docker-v1/<lab-id>/runtime-snapshot`. Snapshot/spec validation precedes
an atomic artifact binding, and Docker receives only a fixed read-only bind of
the snapshot guest directory at `/opt/unified-ext-lab`. Workspace, auth, tool,
and temporary storage remain bounded `noexec` tmpfs mounts. Snapshot removal
is no-follow and descriptor-relative, occurs only after container cleanup,
and is part of clean verification. This makes kills on either side of artifact
binding recoverable without an unowned random temporary directory.

Docker client failure does not prove that an already-submitted
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

Final execution record: commit
`f26aacc0ef603d35fb2837510a4682d4762011ac` on
`codex/platform-stage6-actual-fix` completed the authorized local Docker
conformance run. Docker Desktop 4.82.0 / Engine 29.6.1 used the locked
`linux/amd64` Python base reference
`python@sha256:72d3d75f2639ab82b34b29390ad3d6e0827c775befee94edda8e9976818f488d`;
the immutable local image ID was the same digest.

The final `stage6-e2e-five` run in
`/private/tmp/unified-ext-lab-e2e.FQCQhU` returned exit 0 with phase
`PASSED`, result `passed`, `tainted=false`, and revision 20. Evidence mode
was `0600`; the executor was `real_docker`; `promotion_eligible=false`;
one resource was created and removed; zero remained; and `verified_clean=true`.
All lifecycle operations succeeded and the runtime snapshot was absent after
the run. Post-run managed container, volume, and image label queries each
returned zero.

During the final run, the authoritative local Docker-host proxy log was
unchanged before and after: 287788 bytes, 2247 lines, inode 64776299. This is
observed local evidence supporting no registry/proxy traffic during that run,
not universal proof of its absence.

Offline lab verification completed 254/254 on Apple Python 3.9.6 and Python
3.14.3; the earlier Core result was 615/615. The Python 3.9 Darwin symlink
fallback audit found P0/P1/P2 counts of 0. The prior failed diagnostic roots
`/private/tmp/unified-ext-lab-e2e.jYFpUe` and
`/private/tmp/unified-ext-lab-e2e.2EoEXX` are intentionally preserved as
non-promotional `DIRTY` evidence; no resources remain from them.

No provider installation, authentication, account, or live-vendor validation
was performed.
