# Platform-next Stage 5A foundation audit

Recorded on 2026-07-21 from the `codex/platform-ext-adapters` feature
branch, building on the audited Stage 4 browser-management boundary.

## Scope

Stage 5A adds the provider-neutral adapter foundation inside
`unified-cli-ext`. It does not add or activate a real provider. The Core
package remains unchanged and the Ext package continues to import only its own
namespace.

The new foundation provides:

- immutable, versioned `ProviderAdapterSpecV1` metadata and a metadata-only
  registry;
- explicit binary inspection, version, feature, doctor, model, and auth probe
  contracts;
- fixed argument-vector prompt construction with bounded input modes;
- plain, JSON, JSONL, and JSON-RPC process boundaries;
- explicit workspace and provider-home selection;
- minimal provider-specific environment selection;
- bounded output, diagnostics, cancellation, timeout, and process-group
  cleanup;
- single-use interactive auth handles owned by the REPL runtime; and
- a bounded retained-cleanup registry for factory failures that cannot finish
  cleanup synchronously.

Held adapters cannot inspect, authenticate, build prompts, or execute. ACP and
HTTP provider execution remain unavailable until their complete lifecycle and
permission contracts are implemented. Server access remains disabled for all
Ext adapters.

## Compatibility and lifecycle boundary

- Executables are resolved only from an explicit absolute canonical path.
  Their contents are hashed once at acquisition, while later operations check
  the captured path, file metadata, parent directories, and direct interpreter
  chain without repeated full-file hashing.
- Interpreter chains are bounded. Environment-dispatch shebangs are not
  accepted by this foundation; official installation receipts and supported
  launcher resolution are deferred to the provider cohort stage.
- Adapter metadata has an aggregate 256 KiB UTF-8 limit and recursively frozen
  collections. ABI versions must be exact integers.
- Model discovery uses JSON records only. Plain-text model-list metadata is
  rejected until a bounded list grammar exists.
- Every subprocess uses an explicit workspace, a private temporary HOME unless
  a private persistent HOME was selected, a fixed environment allowlist, and
  separate bounded stdout and stderr handling.
- Start, close, cancellation, and partial-construction paths retain their
  owners until cleanup is complete. A bounded registry keeps ownership when a
  factory cannot return a handle.
- JSONL send and receive retain a lifecycle-checked local process reference.
  If close wins a concurrent operation, the caller receives a stable transport
  error rather than an internal assertion.
- Partial and final protocol output remains bounded and diagnostics use bounded
  redaction work. Provider values are not copied into exception messages.

## Independent audit status

Independent Sol reviews covered the adapter ABI, process lifecycle, resource
ownership, concurrency, cancellation, diagnostics bounds, Python compatibility,
and distribution separation. Earlier reviews found issues in cleanup ownership,
descriptor finalization, repeated executable hashing, diagnostic work bounds,
metadata size, interpreter depth, model-probe format coherence, ABI type
validation, and close/send/receive overlap. Each issue received a deterministic
regression before the final review.

The final implementation-independent review reported:

- P0: **0**
- P1: **0**
- P2: **0**
- Gate: **PASS**
- Stage 5B may advance: **YES**

## Verification

The final post-repair evidence is:

- Core raw suite: **592 passed**, with one pre-existing Starlette/httpx
  deprecation warning
- Ext raw suite with warnings treated as errors: **216 passed, 1 skipped**
- Latest focused release-quality regressions: **7 passed**
- Consolidated lifecycle and bounds regressions: **10 passed**
- Python 3.9 grammar parse: **97 repository Python files passed**
- Python 3.9 runtime import and compile check: passed for the Ext package
- Wheel and sdist metadata: **twine check passed** for Core and Ext
- Distribution contents: Core and Ext wheel paths do not overlap
- Clean-install combination: Core 0.5.0 plus Ext 0.1.0 installed and passed
  dependency and import checks
- Removal recovery: uninstalling Ext left Core 0.5.0 healthy
- `git diff --check`: passed for the recorded candidate
- Post-test process and temporary-root checks: no Stage 5A fixture residue

The single skipped Ext test requires effective UID 0 to exercise an
owner-mismatch condition and is unchanged from the existing platform-specific
test policy.

## Retained limitations

- Stage 5A is infrastructure only. It contains no concrete provider entry
  point and does not claim authenticated compatibility with any vendor CLI.
- Official package identity, installer receipts, symlink/launcher resolution,
  and provider-specific version evidence are Stage 5B requirements.
- ACP and HTTP execution remain closed. Their optional dependencies are still
  lazy and do not expand the base installation.
- Ext providers remain disabled in server mode until a separately reviewed
  permission mapping exists.
- A descendant that creates a separate operating-system session is outside the
  portable process-group boundary; isolated E2E environments provide the outer
  containment layer.
- Pending cleanup is capped at 32 owners and is retried only by explicit drain
  or bounded process-exit cleanup, not by a background worker.

This audit records feature-branch development evidence only. No merge to
`main`, tag, PyPI upload, GitHub Release, provider installation, or account
authentication is authorized by this document.
