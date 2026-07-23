"""Opt-in Preview adapter for the official Cline CLI."""

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


CLINE_OFFICIAL_SOURCES = (
    "https://docs.cline.bot/cli/cli-reference",
    "https://docs.cline.bot/usage/cli-overview",
)
CLINE_DEFAULT_MODEL = "default"
CLINE_OFFICIAL_PACKAGES = ("@cline/cli", "cline")
CLINE_HEADLESS_FIXED_ARGV = ("--auto-approve", "false")

_PROBE_LIMITS = OperationLimits(10.0, 64 * 1024, 16 * 1024, 8)
_PROMPT_LIMITS = OperationLimits(120.0, 16 * 1024 * 1024, 1024 * 1024, 50_000)


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(argv, limits=_PROBE_LIMITS)


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="cline",
    display_name="Cline CLI",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable="cline",
        expected_identity="cline",
        version_probe=VersionProbeSpec(
            _command("--version"),
            minimum_version=(0,),
            format=ProbeFormat.PLAIN_TEXT,
            version_marker="cline ",
        ),
        feature_probe=FeatureProbeSpec(
            _command("--help"),
            required_features=frozenset(("chat",)),
            format=ProbeFormat.PLAIN_TEXT,
            feature_markers={"chat": "prompt"},
            identity_marker="prompt",
            marker_prefixes=True,
            identity_prefix=True,
        ),
    ),
    prompt=PromptCommandSpec(
        fixed_argv=CLINE_HEADLESS_FIXED_ARGV,
        mode=PromptMode.POSITIONAL_AFTER_SENTINEL,
        sentinel_policy=PromptSentinelPolicy.REQUIRED,
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.PLAIN,
    environment=EnvironmentPolicy(
        allowed_keys=frozenset(("CLINE_NO_AUTO_UPDATE",))
    ),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("--version"))),
    capabilities=frozenset((ProviderCapability.CHAT.value,)),
    server_policy=AdapterServerPolicy(enabled=False),
)

PLUGIN = adapter_plugin(
    ADAPTER_SPEC,
    default_model=CLINE_DEFAULT_MODEL,
    launch_resolver=path_launch_resolver(
        provider_id="cline",
        executable="cline",
        package_names=CLINE_OFFICIAL_PACKAGES,
    ),
)


__all__ = [
    "ADAPTER_SPEC",
    "CLINE_DEFAULT_MODEL",
    "CLINE_HEADLESS_FIXED_ARGV",
    "CLINE_OFFICIAL_PACKAGES",
    "CLINE_OFFICIAL_SOURCES",
    "PLUGIN",
]
