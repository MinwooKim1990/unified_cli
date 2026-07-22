# Platform Next Stage 6C accountless-provider validation foundation

Stage 6C adds a source-checkout-only foundation for exercising timestamped,
unverified candidate identities for xAI Grok Build, Kimi Code CLI, GitHub
Copilot CLI, and Cursor Agent CLI. It is additive to the existing synthetic
Stage 6A and 6B harnesses. It does not establish package ownership or official
status, edit an Ext Provider adapter, install a provider, authenticate, access
an account, send a prompt, or make provider evidence promotion-eligible.

The research cutoff for the built-in candidate profiles is 2026-07-22. Their
coordinates and command forms are frozen research inputs, not verified
installation evidence. No primary URL is recorded because this foundation
does not yet carry source evidence that can be bound to such a URL.

## Current routing status

All four real acquisitions are intentionally held. None names a canonical
source-controlled supply manifest. A boolean, regex-shaped digest/count
object, or root npm SRI alone cannot represent readiness. The command and
evidence foundation is runnable with the deterministic in-memory test runner,
while the source-checkout launcher refuses real install before attaching a
network or invoking an installer.

| Provider | Unverified candidate coordinate | Binary | Version form | Help form | Accountless status form | Real install gate |
| --- | --- | --- | --- | --- | --- | --- |
| `grok` | `@xai-official/grok@0.2.106` | `grok` | `grok --version` | `grok --help` | unavailable | pinned npm SRI closure and digest-pinned Node runtime absent |
| `kimi` | `@moonshot-ai/kimi-code@0.29.0` | `kimi` | `kimi --version` | `kimi --help` | unavailable | pinned npm SRI closure and digest-pinned Node runtime absent |
| `copilot` | `@github/copilot@1.0.73` | `copilot` | `copilot --binary-version` | `copilot help` | unavailable | pinned npm SRI closure and digest-pinned Node runtime absent |
| `cursor` | build `2026.07.20-8cc9c0b` | `agent` | `agent --version` | `agent --help` | `agent status` | source-evidenced artifact checksum unavailable |

The Grok profile deliberately names only the candidate xAI-namespaced package
and the `grok` binary. That namespace is not treated as ownership verification.
It does not accept `@vibe-kit/grok-cli` and does not use the candidate Grok
`agent` alias, which would collide with Cursor's candidate primary `agent`
binary.

Profile data is a frozen built-in mapping. A caller supplies only one of the
four provider IDs. No profile, config file, plugin, environment variable, URL,
registry, artifact location, package, version, executable, argv, shell, guest
command, timeout, or working directory can be supplied on the command line.

Install readiness can be issued only by the canonical supply-manifest parser.
A profile's source metadata fixes one basename, expected SHA-256, expected
entry count, and fixture status under the fixed
`tools/unified_ext_lab/locks/provider-supply` root. The parser opens only that
regular, owner-controlled `0644` file without following links, bounds its
size, recomputes its digest, rejects duplicate JSON keys, and requires its
bytes to equal the canonical sorted JSON serialization. It then validates the
exact schema and keys; provider/package/version/acquisition identity; OS and
architecture; base-image digest reference and immutable image ID; Node
version and executable digest; root locator/version/integrity/hash/size; and
every sorted dependency or artifact locator/version/integrity/hash/size. The
declared count must equal the actual root plus closure entries, and empty,
incomplete, duplicate, or mismatched closures are refused. The immutable
parser result, not caller-constructed lock objects, supplies runtime and
acquisition locks. Profile hashes bind the fixed manifest source metadata.

`synthetic-readiness.supply.v1.json` is a small `fixture_only=true`,
`promotion_eligible=false`, `@example` manifest used solely by local parser
and fake-lifecycle tests. Its synthetic provider ID is absent from the CLI
allowlist and it is neither provider evidence nor an install authorization.

## Fixed command grammar

The launcher is `scripts/unified-ext-provider-lab`. Every state path and
evidence path must be canonical and have an already-existing, caller-owned
`0700` parent.

```sh
scripts/unified-ext-provider-lab create \
  --provider grok \
  --lab-id provider-one \
  --state-root /absolute/private/state \
  --json

scripts/unified-ext-provider-lab install \
  --provider grok \
  --lab-id provider-one \
  --state-root /absolute/private/state \
  --allow-network \
  --allow-install \
  --json

scripts/unified-ext-provider-lab test \
  --provider grok \
  --lab-id provider-one \
  --state-root /absolute/private/state \
  --json

scripts/unified-ext-provider-lab evidence \
  --provider grok \
  --lab-id provider-one \
  --state-root /absolute/private/state \
  --json

scripts/unified-ext-provider-lab logout \
  --provider grok \
  --lab-id provider-one \
  --state-root /absolute/private/state \
  --json

scripts/unified-ext-provider-lab destroy \
  --provider grok \
  --lab-id provider-one \
  --state-root /absolute/private/state \
  --json

scripts/unified-ext-provider-lab verify-clean \
  --provider grok \
  --lab-id provider-one \
  --state-root /absolute/private/state \
  --evidence-output /absolute/private/result.json \
  --json
```

The intended lifecycle is:

```text
create -> install -> test -> evidence -> logout -> destroy -> verify-clean
```

`install` is the only provider operation that could temporarily enable
network access, and it requires both exact flags. The lifecycle also requires
a parser-issued immutable result from a complete canonical supply manifest
before it constructs a network command. Current profiles have no manifest
source metadata and therefore return the stable unsupported exit before Docker
discovery, without running Docker network or installer commands. Cursor
remains fail-closed until a source-evidenced or otherwise authorized exact
SHA-256 is pinned in such a manifest; the lab never falls back to unchecked
`curl | tar`.

No command exposes an interactive shell. A shell would permit unbounded argv,
environment, filesystem, network, and credential access that the evidence
schema cannot represent or verify. Adding a shell would invalidate the exact
command grammar and is intentionally unsupported rather than tainted-and-run.

## Docker and cleanup boundary

The provider path reuses Stage 6B's exact-ID container lifecycle. The container
is always created with:

- UID/GID `65532:65532`;
- a read-only root filesystem;
- every capability dropped and `no-new-privileges=true`;
- `--network none` outside a fully locked install window;
- bounded CPU, memory, swap, PIDs, open files, command timeout, and captured
  output;
- bounded `noexec,nosuid,nodev` tmpfs mounts for `/tmp`, `/workspace`,
  `/home/lab`, and `/opt/unified-ext-lab/tool`;
- no host HOME, Keychain, SSH material, git configuration or credentials,
  credential helper, Docker socket, or arbitrary bind mount.

The only bind is the private, hash-locked, state-derived guest snapshot. The
caller cannot select it. A restarted forward runtime uses a read-only loader
that validates the persisted parent, lock, complete inventory, ownership,
modes, and hashes; it never calls the snapshot creation path. One lab plans
only one provider-scoped container, with the full random ownership token in
its exact name and labels. Once Docker
returns an immutable container ID, cleanup targets only that ID. A renamed,
relabeled, duplicated, foreign, or unrelated resource is never substituted or
removed. The derived snapshot is removed only after exact container cleanup,
and clean verification requires both resources to be absent.

If a future fully locked install submits the fixed network-connect mutation,
the lifecycle always submits a forced disconnect for that same immutable
container ID, including when the connect result itself is uncertain. Probe
work cannot begin until connect reports success, and any connect, install,
disconnect, or final identity-check failure moves the lab to cleanup-only
recovery.

The provider snapshot also carries an internal/external same-inode identity
marker. Cleanup validates the pair before touching the canonical directory,
durably records snapshot-removal intent before the filesystem mutation, keeps
the remaining one-link marker as a crash tombstone, records removal, and only
then removes that exact tombstone. First-attempt name absence, same-name
replacement, or a removal race taints the lab and never triggers a directory
scan; only a retry carrying the prior durable removal intent and tombstone may
reconcile an exact crash-after-remove. A checkout update may change the current
candidate profile, but cleanup reconstructs authority only from the strictly
validated persisted lab/provider/token/resource identity and keeps the
original profile artifact in evidence.

`HostConfig.NetworkMode=none` is not accepted as sufficient proof of offline
state. The create check, post-disconnect check, and the check immediately
before every accountless provider probe also inspect
`NetworkSettings.Networks`; any attachment other than an inert `none`
endpoint is refused. A disconnect command that reports success but leaves an
endpoint moves the lab into cleanup-only recovery and no probe is run.

Accountless `logout` never runs a vendor logout command. No host or provider
credentials are mounted, and provider HOME is a per-container tmpfs. Running a
vendor logout would create an unnecessary future risk of mutating an unrelated
signed-in account; destroying the exact container discards the accountless
tmpfs instead.

## Evidence meaning

Stage 6C evidence has the fixed pair:

```text
evidence_kind=provider_accountless
executor_kind=provider_accountless_docker
promotion_eligible=false
```

The artifact entry identifies the immutable source-controlled candidate
profile and its SHA-256, not a verified provider identity, credentialed
response, or package download. Evidence records only the provider/lab IDs,
immutable profile identity, schema hashes, bounded operation outcomes and
latencies, cleanup counts, result, and capture time. Validation and state
serialization reject
argv, stdout, stderr, prompts, responses, environment values, accounts,
sessions, credentials, host paths, process IDs, URLs, and receipt contents.

Final evidence is create-only and can be sealed only after exact cleanup is
verified with zero remaining owned resources. It can never move a provider
from Held, authorize a release, or prove authentication, entitlement, paid
model access, prompt behavior, session behavior, tool denial, update denial,
or provider protocol compatibility. Those require a separate credentialed
provider E2E approval and schema.

## Remaining gates

Before any real provider install can be enabled:

1. Add a canonical, source-controlled manifest whose fixed source metadata
   binds its complete bytes and actual entry count. Pin a digest-locked
   Node-capable base and validate its local immutable image ID, OS,
   architecture, Node version, and executable digest without mutable tags.
2. Add an audited immutable execution layout. The current writable tool tmpfs
   is deliberately `noexec`; provider code must remain data consumed by a
   pinned read-only interpreter, or be baked into a new digest-verified
   read-only image layer. It must never be made executable by weakening the
   tmpfs or mounting a host tool directory.
3. For Grok, Kimi, and Copilot, record the root npm SRI/hash/size plus every
   transitive/platform package's exact locator, version, SRI, hash, and size in
   that canonical manifest. Installation must keep scripts disabled unless a
   separately audited exact script is required and pinned.
4. For Cursor, obtain a source-evidenced or otherwise authorized exact artifact
   locator, version, SHA-256, and size and bind them into the canonical
   platform manifest. Absence continues to mean refusal.
5. Add provider-specific, accountless real-Docker fixtures for exact version,
   help, and documented status outcomes on each supported architecture.
6. Demonstrate that version/help/status perform no update, telemetry, config,
   account, or network mutation while Docker network remains absent.
7. Run credentialed provider E2E separately before considering any adapter
   promotion. Stage 6C evidence remains non-promotional even after accountless
   probes pass.

No real network, provider installation, login, logout, prompt, or provider API
call was performed while implementing this foundation.
