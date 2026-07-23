"""Opt-in Preview adapter for the official Kimi Code CLI."""

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


KIMI_OFFICIAL_SOURCES = (
    "https://moonshotai.github.io/kimi-code/en/guides/getting-started.html",
    "https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html",
)
KIMI_OFFICIAL_PACKAGE = "@moonshot-ai/kimi-code"
KIMI_STAGE_6_TARGET_VERSION = "0.29.0"
KIMI_NPM_MINIMUM_NODE_VERSION = "22.19"
KIMI_DEFAULT_MODEL = "default"
KIMI_DOCUMENTED_HEADLESS_FIXED_ARGV = ("--output-format", "text")

_PROBE_LIMITS = OperationLimits(10.0, 64 * 1024, 16 * 1024, 8)
_PROMPT_LIMITS = OperationLimits(120.0, 16 * 1024 * 1024, 1024 * 1024, 50_000)


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(argv, limits=_PROBE_LIMITS)


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="kimi",
    display_name="Kimi Code CLI",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable="kimi",
        expected_identity="kimi",
        version_probe=VersionProbeSpec(
            _command("--version"),
            minimum_version=(0,),
            format=ProbeFormat.PLAIN_TEXT,
            version_marker="kimi ",
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
        fixed_argv=KIMI_DOCUMENTED_HEADLESS_FIXED_ARGV,
        mode=PromptMode.OPTION_VALUE,
        prompt_option="-p",
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.PLAIN,
    environment=EnvironmentPolicy(
        allowed_keys=frozenset(
            ("KIMI_CODE_NO_AUTO_UPDATE", "KIMI_DISABLE_TELEMETRY")
        )
    ),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("--version"))),
    capabilities=frozenset((ProviderCapability.CHAT.value,)),
    server_policy=AdapterServerPolicy(enabled=False),
)

PLUGIN = adapter_plugin(
    ADAPTER_SPEC,
    default_model=KIMI_DEFAULT_MODEL,
    launch_resolver=path_launch_resolver(
        provider_id="kimi",
        executable="kimi",
        package_names=(KIMI_OFFICIAL_PACKAGE,),
    ),
)


__all__ = [
    "ADAPTER_SPEC",
    "KIMI_DEFAULT_MODEL",
    "KIMI_DOCUMENTED_HEADLESS_FIXED_ARGV",
    "KIMI_OFFICIAL_PACKAGE",
    "KIMI_OFFICIAL_SOURCES",
    "PLUGIN",
]
