"""Opt-in Preview adapter for the official OpenCode CLI."""

from __future__ import annotations

from .bridge import adapter_plugin
from .contract import (
    AdapterServerPolicy,
    AdapterStatus,
    BinarySpec,
    DoctorProbeSpec,
    EnvironmentPolicy,
    ExitStatusProbeSpec,
    FeatureProbeSpec,
    FixedCommandSpec,
    OperationLimits,
    ProbeFormat,
    PromptCommandSpec,
    PromptMode,
    PromptSentinelPolicy,
    ProviderAdapterSpecV1,
    ProviderCapability,
    TransportKind,
    VersionProbeSpec,
)
from .path_resolver import path_launch_resolver


OPENCODE_OFFICIAL_SOURCES = ("https://opencode.ai/docs/cli/",)
OPENCODE_DEFAULT_MODEL = "default"
OPENCODE_OFFICIAL_PACKAGE = "opencode-ai"
OPENCODE_HEADLESS_FIXED_ARGV = ("run",)
OPENCODE_FIXED_ENVIRONMENT = {
    "OPENCODE_DISABLE_AUTOUPDATE": "true",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "true",
    "OPENCODE_DISABLE_MODELS_FETCH": "true",
    "OPENCODE_DISABLE_CLAUDE_CODE": "true",
}

_PROBE_LIMITS = OperationLimits(10.0, 64 * 1024, 16 * 1024, 8)
_PROMPT_LIMITS = OperationLimits(120.0, 16 * 1024 * 1024, 1024 * 1024, 50_000)


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(argv, limits=_PROBE_LIMITS)


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="opencode",
    display_name="OpenCode",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable="opencode",
        expected_identity="opencode",
        version_probe=VersionProbeSpec(
            _command("--version"),
            minimum_version=(0,),
            format=ProbeFormat.PLAIN_TEXT,
            version_is_entire_line=True,
        ),
        feature_probe=FeatureProbeSpec(
            _command("--help"),
            required_features=frozenset(("chat",)),
            format=ProbeFormat.PLAIN_TEXT,
            feature_markers={"chat": "opencode run [message..]"},
            identity_marker="opencode run [message..]",
            marker_prefixes=True,
            identity_prefix=True,
            use_stderr=True,
        ),
    ),
    prompt=PromptCommandSpec(
        fixed_argv=OPENCODE_HEADLESS_FIXED_ARGV,
        mode=PromptMode.POSITIONAL_AFTER_SENTINEL,
        sentinel_policy=PromptSentinelPolicy.REQUIRED,
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.PLAIN,
    environment=EnvironmentPolicy(fixed_values=OPENCODE_FIXED_ENVIRONMENT),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("--version"))),
    capabilities=frozenset((ProviderCapability.CHAT.value,)),
    server_policy=AdapterServerPolicy(enabled=False),
)

PLUGIN = adapter_plugin(
    ADAPTER_SPEC,
    default_model=OPENCODE_DEFAULT_MODEL,
    launch_resolver=path_launch_resolver(
        provider_id="opencode",
        executable="opencode",
        package_names=(OPENCODE_OFFICIAL_PACKAGE,),
    ),
)


__all__ = [
    "ADAPTER_SPEC",
    "OPENCODE_DEFAULT_MODEL",
    "OPENCODE_FIXED_ENVIRONMENT",
    "OPENCODE_HEADLESS_FIXED_ARGV",
    "OPENCODE_OFFICIAL_PACKAGE",
    "OPENCODE_OFFICIAL_SOURCES",
    "PLUGIN",
]
