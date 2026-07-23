"""Opt-in Preview adapter for the official CodeBuddy Code CLI."""

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


CODEBUDDY_OFFICIAL_SOURCES = (
    "https://www.codebuddy.ai/docs/cli/reference",
    "https://www.codebuddy.ai/docs/cli/headless",
)
CODEBUDDY_DEFAULT_MODEL = "default"
CODEBUDDY_OFFICIAL_PACKAGE = "@tencent-ai/codebuddy-code"
CODEBUDDY_HEADLESS_FIXED_ARGV = (
    "--output-format",
    "text",
    "--permission-mode",
    "dontAsk",
    "--strict-mcp-config",
)

_PROBE_LIMITS = OperationLimits(10.0, 64 * 1024, 16 * 1024, 8)
_PROMPT_LIMITS = OperationLimits(120.0, 16 * 1024 * 1024, 1024 * 1024, 50_000)


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(argv, limits=_PROBE_LIMITS)


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="codebuddy",
    display_name="CodeBuddy Code",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable="codebuddy",
        expected_identity="codebuddy",
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
            feature_markers={"chat": "-p, --print"},
            identity_marker="-p, --print",
            marker_prefixes=True,
            identity_prefix=True,
        ),
    ),
    prompt=PromptCommandSpec(
        fixed_argv=CODEBUDDY_HEADLESS_FIXED_ARGV,
        mode=PromptMode.OPTION_VALUE,
        prompt_option="-p",
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.PLAIN,
    environment=EnvironmentPolicy(
        allowed_keys=frozenset(("DISABLE_AUTOUPDATER",))
    ),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("--version"))),
    capabilities=frozenset((ProviderCapability.CHAT.value,)),
    server_policy=AdapterServerPolicy(enabled=False),
)

PLUGIN = adapter_plugin(
    ADAPTER_SPEC,
    default_model=CODEBUDDY_DEFAULT_MODEL,
    launch_resolver=path_launch_resolver(
        provider_id="codebuddy",
        executable="codebuddy",
        package_names=(CODEBUDDY_OFFICIAL_PACKAGE,),
    ),
)


__all__ = [
    "ADAPTER_SPEC",
    "CODEBUDDY_DEFAULT_MODEL",
    "CODEBUDDY_HEADLESS_FIXED_ARGV",
    "CODEBUDDY_OFFICIAL_PACKAGE",
    "CODEBUDDY_OFFICIAL_SOURCES",
    "PLUGIN",
]
