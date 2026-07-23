"""Opt-in Preview adapter for the official Amp CLI."""

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
    ProviderAdapterSpecV1,
    ProviderCapability,
    TransportKind,
    VersionProbeSpec,
)
from .path_resolver import path_launch_resolver


AMP_OFFICIAL_SOURCES = (
    "https://ampcode.com/manual",
    "https://ampcode.com/manual/appendix",
)
AMP_DEFAULT_MODEL = "default"
AMP_OFFICIAL_PACKAGE = "@ampcode/cli"
AMP_HEADLESS_FIXED_ARGV = ("--execute",)

_PROBE_LIMITS = OperationLimits(10.0, 64 * 1024, 16 * 1024, 8)
_PROMPT_LIMITS = OperationLimits(120.0, 16 * 1024 * 1024, 1024 * 1024, 50_000)


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(argv, limits=_PROBE_LIMITS)


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="amp",
    display_name="Amp CLI",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable="amp",
        expected_identity="amp",
        version_probe=VersionProbeSpec(
            _command("--version"),
            minimum_version=(0,),
            format=ProbeFormat.PLAIN_TEXT,
            version_marker="amp ",
        ),
        feature_probe=FeatureProbeSpec(
            _command("--help"),
            required_features=frozenset(("chat",)),
            format=ProbeFormat.PLAIN_TEXT,
            feature_markers={"chat": "-x, --execute"},
            identity_marker="-x, --execute",
            marker_prefixes=True,
            identity_prefix=True,
        ),
    ),
    prompt=PromptCommandSpec(
        fixed_argv=AMP_HEADLESS_FIXED_ARGV,
        mode=PromptMode.STDIN,
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.PLAIN,
    environment=EnvironmentPolicy(
        allowed_keys=frozenset(("AMP_API_KEY", "AMP_SKIP_UPDATE_CHECK"))
    ),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("--version"))),
    capabilities=frozenset((ProviderCapability.CHAT.value,)),
    server_policy=AdapterServerPolicy(enabled=False),
)

PLUGIN = adapter_plugin(
    ADAPTER_SPEC,
    default_model=AMP_DEFAULT_MODEL,
    launch_resolver=path_launch_resolver(
        provider_id="amp",
        executable="amp",
        package_names=(AMP_OFFICIAL_PACKAGE,),
    ),
)


__all__ = [
    "ADAPTER_SPEC",
    "AMP_DEFAULT_MODEL",
    "AMP_HEADLESS_FIXED_ARGV",
    "AMP_OFFICIAL_PACKAGE",
    "AMP_OFFICIAL_SOURCES",
    "PLUGIN",
]
