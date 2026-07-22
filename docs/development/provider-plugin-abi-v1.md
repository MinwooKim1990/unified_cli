# Core provider plugin ABI v1

Provider extensions let a separately installed Python distribution provide an
additional `unified-cli` provider.  ABI v1 is intentionally narrow: it is an
explicit, lazy extension boundary rather than a way to alter core provider
routing or server policy.

## Entry point

Publish exactly one Python entry point per provider in the
`unified_cli.providers.v1` group.  Its entry-point name **must equal the
plugin's `id`**.  Loading that entry point must yield a `ProviderPluginV1`
instance.

For example, a future distribution could declare the following in its
`pyproject.toml` (the `acme-unified-cli` package in this example is not
currently published):

```toml
[project.entry-points."unified_cli.providers.v1"]
acme = "acme_unified_cli.plugin:PLUGIN"
```

`PLUGIN` in that example is a module-level `ProviderPluginV1` value whose
`id` is `"acme"`.

## `ProviderPluginV1` contract

`ProviderPluginV1` is an immutable dataclass for ABI version 1.  An extension
must supply these implementation fields:

- `id`: a valid provider id, and the same value as the entry-point name.
- `factory`: a callable used to construct the provider.  Core calls it with a
  `model` keyword (the requested model or `default_model`) and any options
  supplied to `create`.
- `default_model`: a non-empty model string.
- `model_lister`: a zero-argument callable returning at most 1,000 unique
  `ModelInfo` values for this provider. IDs and display names must be bounded,
  valid strict UTF-8, free of Unicode control/line-separator characters, and
  use `source="plugin"`; there may be at most one default model.
- `doctor`: a zero-argument callable for provider diagnostics.

It also has these ABI metadata fields:

- `capabilities`: an iterable of valid capability names, normalized to a
  `frozenset`.
- `route_prefixes`: retained for ABI shape compatibility, but in ABI v1 it is
  exactly `(id,)`.  Omitting it normalizes it to `(id,)`; aliases and extra
  prefixes are rejected.
- `server_policy`: a `ProviderServerPolicyV1` metadata value.  Its `enabled`
  and `requires_external_isolation` fields do not authorize HTTP-server use.
- `support_status`: one of `stable`, `preview`, `experimental`, or `held`.
  This describes integration maturity and is separate from registry lifecycle.
  It defaults to `experimental` so omission does not imply completed
  compatibility evidence while existing plugin constructors remain usable.
  A `held` plugin must advertise no Core capabilities; Core stops explicit
  create, model-list, and doctor operations before calling plugin code.
- `abi_version`: must be `1`.

ABI v1 also has an additive, opt-in configuration sub-ABI. A plugin that
implements it sets all three of these tail fields:

- `configuration_abi_version`: exactly
  `PROVIDER_CONFIGURATION_ABI_V1` (`1`). Omitting it preserves the original
  ABI-v1 callback behavior.
- `launch_binder`: a callable accepting one `ProviderLaunchContextV1` and
  returning exactly one `BoundProviderOperationsV1`.
- `environment_keys`: at most 64 uppercase environment names that the binder
  is prepared to receive, normalized to a `frozenset`.

Supplying only part of that metadata is invalid. A `held` plugin cannot opt in
to the configuration sub-ABI.

Provider ids `claude`, `codex`, `gemini`, and `agy` are reserved.  Extensions
must not use any of them. IDs beginning with Core model-routing prefixes
(`claude-`, `gpt-`, `o1-`, `o3-`, `codex-`, or `gemini-`) are also invalid;
reserving those namespaces keeps historical Core inference deterministic and
free of extension discovery.

## How callers reach an extension

Extensions are requested explicitly:

- `create(id)` loads that one extension and invokes its `factory`.
- `route("id/model")` performs an exact metadata-only entry-point lookup for
  `id`; it does not load the plugin.  ABI v1 has no extension aliases and no
  unprefixed extension model-name inference.
- `list_models(id)` loads that one extension and invokes its `model_lister`.
- `doctor_provider(id)` loads that one extension and invokes its `doctor`.
- `unified-cli providers --include-ext` enumerates extension entry-point
  metadata in addition to the built-ins.

The core `PROVIDERS` mapping remains limited to `claude`, `codex`, and
`gemini`.  Built-in `create`, built-in routing/inference, and the default
provider listing are fast paths: they do not discover entry points or load
extension modules.  `--help` and `--version` also do not trigger discovery.

`providers --include-ext` is deliberately metadata-only.  It enumerates entry
points without importing their modules, so an un-loaded extension is shown as
`lifecycle_status="discovered"` and `support_status="unknown"` based on its
entry-point name only. Consequently it cannot verify the plugin object's
fields or expose its default model and capabilities. Metadata for an extension
already loaded in the current process is shown after validation, with lifecycle
and support status reported separately. `status` remains a compatibility alias
for `lifecycle_status` in the JSON descriptor.
An entry-point initializer must not recursively load another provider. Core
rejects nested loads so circular imports cannot deadlock registry threads.

## Typed launch configuration

The configuration sub-ABI uses the following public Core types:

- `ProviderReceiptEnvelopeV1(provider_id, media_type, payload)` carries a
  format-tagged, bounded JSON mapping. Core copies it into immutable
  containers, but the plugin owns the media type, strict decoder, and
  installation-evidence verification. A normalized persistent receipt must
  not contain credentials, authenticated URLs, or request-local environment
  values.
- `ExtensionLaunchOverridesV1(receipt=None, bin_path=None,
  provider_home=None, extra_env={})` is the caller-owned explicit input.
  `receipt` and `bin_path` are mutually exclusive; paths must be normalized
  absolute paths. This type is never an instruction to inspect ambient
  environment variables.
- `ProviderLaunchContextV1` is Core's immutable snapshot after explicit
  overrides are merged over stored receipt and home metadata. Core supplies
  only explicitly provided environment entries.
- `ProviderCreateRequestV1` contains the validated model, absolute workspace,
  optional timeout, and bounded runtime-limit overrides for one create call.
- `BoundProviderOperationsV1` contains `factory`, `model_lister`, and `doctor`
  callbacks closed over that same launch snapshot, plus the normalized receipt
  and provider home. Core reconstructs the returned value at the boundary.

The binder must validate or capture the receipt before returning and must make
all three callbacks use the same receipt, environment, and provider home. Its
factory accepts the typed create request rather than arbitrary keyword
arguments. Core validates create options before invoking the binder and
rejects unsupported options.

`environment_keys` is a strict allowlist for typed launch input. Every key in
`ExtensionLaunchOverridesV1.extra_env` must be declared; an unknown or
misspelled key rejects the launch with a generic configuration error before
Core reads stored settings, creates a default provider home, or calls plugin
code. Declared values are bounded and request-local. They are neither sourced
from `os.environ` nor persisted. This rule does not change the original ABI-v1
factory contract or a plugin's own legacy environment policy.

Configured extensions are available through these public APIs:

- `bind_extension_provider(id, extension_launch=...)` returns the
  Core-reconstructed bound operations without persisting them.
- `configure_extension_provider(id, extension_launch=..., verify=True)` binds
  the launch, optionally runs the bound doctor, and persists the binder's
  normalized receipt and provider home. A doctor result containing
  `available=False`, or a non-boolean `available`, rejects configuration.
- `clear_extension_provider_configuration(id)` removes persisted launch state
  without discovering or importing the provider plugin.
- `create`, `list_models`, and `doctor_provider` accept `extension_launch` for
  an explicitly named extension. With no explicit override, a configured
  extension automatically reuses its stored receipt and home. Legacy plugins
  continue to use their original factory, model-lister, and doctor callbacks.

Configuration errors crossing this boundary are sanitized. Configured doctor
results are copied into bounded Core-owned JSON data; the original ABI-v1
doctor return type remains `Any` and is returned unchanged for legacy plugins.

### Receipt persistence and clear semantics

Core stores a configured receipt as canonical UTF-8 JSON in a private,
content-addressed file below
`~/.unified-cli/providers/<id>/receipt-v1-<sha256>.json`. The ordinary settings
document contains only a typed pointer with the receipt digest and optional
provider-home path. Core publishes the immutable receipt without overwriting
an existing path, fsyncs it first, and only then atomically publishes the
settings pointer. Loads verify the pointer digest, canonical encoding,
provider id, ownership, file type, link count, and private permissions before
the plugin can decode the receipt.

Clearing configuration removes only the settings pointer. It intentionally
leaves the content-addressed receipt blob and provider-home directory in place:
portable POSIX pathname deletion cannot safely bind an unlink to the inode
that was previously validated. Once unreferenced, the receipt blob is inert
and cannot be selected by configured launch. Request-local environment data is
never written to either location.

## Disabling extensions and server boundary

Set `UNIFIED_CLI_DISABLE_PLUGINS=1` to disable extension discovery and loading
for the process.  Built-in providers continue to work.  The registry also
recognizes `true`, `yes`, and `on` (case-insensitive) as enabled values for
this switch.

The local HTTP server always rejects extension providers in ABI v1, before it
performs extension discovery, routing, or loading. It deliberately returns the
same 400 response used for an unknown model prefix so it neither changes the
existing `/v1` contract nor reveals whether an extension is installed. A
plugin's `server_policy` is descriptive metadata only and cannot override that
rule.

## Runtime ownership

An extension runs in the same Python process as the host when it is loaded.
Installing one therefore grants it the process's effective permissions.  Only
install provider distributions whose source and ownership you have verified,
and treat entry-point loading as part of your Python environment's package
provenance boundary. Core
wraps the four public runtime calls (`chat`, `stream`, `achat`, and `astream`)
and converts unexpected extension exceptions to a generic error without
retaining their text or traceback context. This boundary does not isolate
plugin code from the host Python process.
