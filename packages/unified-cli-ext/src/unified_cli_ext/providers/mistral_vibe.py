"""Opt-in Preview adapter for the official Mistral Vibe CLI."""

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


MISTRAL_VIBE_OFFICIAL_SOURCES = (
    "https://docs.mistral.ai/vibe/code/cli/work-with-cli",
    "https://github.com/mistralai/mistral-vibe",
)
MISTRAL_VIBE_DEFAULT_MODEL = "default"
MISTRAL_VIBE_HEADLESS_FIXED_ARGV = (
    "--output",
    "text",
    "--agent",
    "plan",
    "--disabled-tools",
    "*",
)

_PROBE_LIMITS = OperationLimits(10.0, 64 * 1024, 16 * 1024, 8)
_PROMPT_LIMITS = OperationLimits(120.0, 16 * 1024 * 1024, 1024 * 1024, 50_000)


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(argv, limits=_PROBE_LIMITS)


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="mistral-vibe",
    display_name="Mistral Vibe",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable="vibe",
        expected_identity="vibe",
        version_probe=VersionProbeSpec(
            _command("--version"),
            minimum_version=(0,),
            format=ProbeFormat.PLAIN_TEXT,
            version_marker="vibe ",
        ),
        feature_probe=FeatureProbeSpec(
            _command("--help"),
            required_features=frozenset(("chat",)),
            format=ProbeFormat.PLAIN_TEXT,
            feature_markers={"chat": "--prompt"},
            identity_marker="--prompt",
            marker_prefixes=True,
            identity_prefix=True,
        ),
    ),
    prompt=PromptCommandSpec(
        fixed_argv=MISTRAL_VIBE_HEADLESS_FIXED_ARGV,
        mode=PromptMode.OPTION_VALUE,
        prompt_option="--prompt",
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.PLAIN,
    environment=EnvironmentPolicy(
        allowed_keys=frozenset(("MISTRAL_API_KEY",))
    ),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("--version"))),
    capabilities=frozenset((ProviderCapability.CHAT.value,)),
    server_policy=AdapterServerPolicy(enabled=False),
)

PLUGIN = adapter_plugin(
    ADAPTER_SPEC,
    default_model=MISTRAL_VIBE_DEFAULT_MODEL,
    launch_resolver=path_launch_resolver(
        provider_id="mistral-vibe",
        executable="vibe",
    ),
)


__all__ = [
    "ADAPTER_SPEC",
    "MISTRAL_VIBE_DEFAULT_MODEL",
    "MISTRAL_VIBE_HEADLESS_FIXED_ARGV",
    "MISTRAL_VIBE_OFFICIAL_SOURCES",
    "PLUGIN",
]
