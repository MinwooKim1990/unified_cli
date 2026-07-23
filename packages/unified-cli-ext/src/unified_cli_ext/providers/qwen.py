"""Opt-in Preview adapter for the official Qwen Code CLI."""

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


QWEN_OFFICIAL_SOURCES = (
    "https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/",
)
QWEN_DEFAULT_MODEL = "default"
QWEN_OFFICIAL_PACKAGE = "@qwen-code/qwen-code"
QWEN_HEADLESS_FIXED_ARGV = (
    "--safe-mode",
    "--approval-mode",
    "plan",
    "--output-format",
    "text",
)

_PROBE_LIMITS = OperationLimits(10.0, 64 * 1024, 16 * 1024, 8)
_PROMPT_LIMITS = OperationLimits(120.0, 16 * 1024 * 1024, 1024 * 1024, 50_000)


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(argv, limits=_PROBE_LIMITS)


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="qwen",
    display_name="Qwen Code",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable="qwen",
        expected_identity="qwen",
        version_probe=VersionProbeSpec(
            _command("--version"),
            minimum_version=(0,),
            format=ProbeFormat.PLAIN_TEXT,
            version_marker="qwen ",
        ),
        feature_probe=FeatureProbeSpec(
            _command("--help"),
            required_features=frozenset(("chat",)),
            format=ProbeFormat.PLAIN_TEXT,
            feature_markers={"chat": "-p, --prompt"},
            identity_marker="-p, --prompt",
            marker_prefixes=True,
            identity_prefix=True,
        ),
    ),
    prompt=PromptCommandSpec(
        fixed_argv=QWEN_HEADLESS_FIXED_ARGV,
        mode=PromptMode.OPTION_VALUE,
        prompt_option="--prompt",
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.PLAIN,
    environment=EnvironmentPolicy(),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("--version"))),
    capabilities=frozenset((ProviderCapability.CHAT.value,)),
    server_policy=AdapterServerPolicy(enabled=False),
)

PLUGIN = adapter_plugin(
    ADAPTER_SPEC,
    default_model=QWEN_DEFAULT_MODEL,
    launch_resolver=path_launch_resolver(
        provider_id="qwen",
        executable="qwen",
        package_names=(QWEN_OFFICIAL_PACKAGE,),
    ),
)


__all__ = [
    "ADAPTER_SPEC",
    "PLUGIN",
    "QWEN_DEFAULT_MODEL",
    "QWEN_HEADLESS_FIXED_ARGV",
    "QWEN_OFFICIAL_PACKAGE",
    "QWEN_OFFICIAL_SOURCES",
]
