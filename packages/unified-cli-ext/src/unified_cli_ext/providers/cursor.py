"""Opt-in Preview adapter for the official Cursor Agent CLI."""

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


CURSOR_OFFICIAL_SOURCES = (
    "https://cursor.com/docs/cli/installation",
    "https://cursor.com/docs/cli/reference/parameters",
    "https://cursor.com/docs/cli/reference/output-format",
)
CURSOR_PRIMARY_EXECUTABLE = "agent"
CURSOR_LEGACY_EXECUTABLE = "cursor-agent"
CURSOR_DEFAULT_MODEL = "default"
CURSOR_DOCUMENTED_PRINT_OPTIONS = ("--print", "--output-format", "text")

_PROBE_LIMITS = OperationLimits(10.0, 64 * 1024, 16 * 1024, 8)
_PROMPT_LIMITS = OperationLimits(120.0, 16 * 1024 * 1024, 1024 * 1024, 50_000)


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(argv, limits=_PROBE_LIMITS)


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="cursor",
    display_name="Cursor Agent CLI",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable=CURSOR_PRIMARY_EXECUTABLE,
        expected_identity="agent",
        version_probe=VersionProbeSpec(
            _command("--version"),
            minimum_version=(0,),
            format=ProbeFormat.PLAIN_TEXT,
            version_marker="agent ",
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
        fixed_argv=CURSOR_DOCUMENTED_PRINT_OPTIONS,
        mode=PromptMode.STDIN,
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.PLAIN,
    environment=EnvironmentPolicy(allowed_keys=frozenset(("CURSOR_API_KEY",))),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("--version"))),
    capabilities=frozenset((ProviderCapability.CHAT.value,)),
    server_policy=AdapterServerPolicy(enabled=False),
)

PLUGIN = adapter_plugin(
    ADAPTER_SPEC,
    default_model=CURSOR_DEFAULT_MODEL,
    launch_resolver=path_launch_resolver(
        provider_id="cursor",
        executable=CURSOR_PRIMARY_EXECUTABLE,
    ),
)


__all__ = [
    "ADAPTER_SPEC",
    "CURSOR_DEFAULT_MODEL",
    "CURSOR_DOCUMENTED_PRINT_OPTIONS",
    "CURSOR_LEGACY_EXECUTABLE",
    "CURSOR_OFFICIAL_SOURCES",
    "CURSOR_PRIMARY_EXECUTABLE",
    "PLUGIN",
]
