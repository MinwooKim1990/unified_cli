"""Opt-in Preview adapter for the official GitHub Copilot CLI."""

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


COPILOT_OFFICIAL_SOURCES = (
    "https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference",
    "https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference",
    "https://github.com/github/copilot-cli",
)
COPILOT_OFFICIAL_PACKAGE = "@github/copilot"
COPILOT_DEFAULT_MODEL = "auto"
COPILOT_READ_ONLY_TOOLS = ("view", "glob", "grep")
COPILOT_DOCUMENTED_HEADLESS_FIXED_ARGV = (
    "--silent",
    "--no-ask-user",
    "--no-auto-update",
    "--no-custom-instructions",
    "--no-remote",
    "--no-remote-export",
    "--disable-builtin-mcps",
    "--available-tools",
    ",".join(COPILOT_READ_ONLY_TOOLS),
    "--deny-tool=write",
    "--deny-tool=shell",
    "--deny-tool=url",
    "--deny-tool=memory",
    "--output-format=text",
)

_PROBE_LIMITS = OperationLimits(10.0, 64 * 1024, 16 * 1024, 8)
_PROMPT_LIMITS = OperationLimits(120.0, 16 * 1024 * 1024, 1024 * 1024, 50_000)


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(argv, limits=_PROBE_LIMITS)


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="copilot",
    display_name="GitHub Copilot CLI",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable="copilot",
        expected_identity="copilot",
        version_probe=VersionProbeSpec(
            _command("--version"),
            minimum_version=(0,),
            format=ProbeFormat.PLAIN_TEXT,
            version_marker="copilot ",
        ),
        feature_probe=FeatureProbeSpec(
            _command("help"),
            required_features=frozenset(("chat",)),
            format=ProbeFormat.PLAIN_TEXT,
            feature_markers={"chat": "-p, --prompt"},
            identity_marker="-p, --prompt",
            marker_prefixes=True,
            identity_prefix=True,
        ),
    ),
    prompt=PromptCommandSpec(
        fixed_argv=COPILOT_DOCUMENTED_HEADLESS_FIXED_ARGV,
        mode=PromptMode.OPTION_VALUE,
        prompt_option="-p",
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.PLAIN,
    environment=EnvironmentPolicy(allowed_keys=frozenset(("COPILOT_HOME",))),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("--version"))),
    capabilities=frozenset((ProviderCapability.CHAT.value,)),
    server_policy=AdapterServerPolicy(enabled=False),
)

PLUGIN = adapter_plugin(
    ADAPTER_SPEC,
    default_model=COPILOT_DEFAULT_MODEL,
    launch_resolver=path_launch_resolver(
        provider_id="copilot",
        executable="copilot",
        package_names=(COPILOT_OFFICIAL_PACKAGE,),
    ),
)


__all__ = [
    "ADAPTER_SPEC",
    "COPILOT_DEFAULT_MODEL",
    "COPILOT_DOCUMENTED_HEADLESS_FIXED_ARGV",
    "COPILOT_OFFICIAL_PACKAGE",
    "COPILOT_OFFICIAL_SOURCES",
    "COPILOT_READ_ONLY_TOOLS",
    "PLUGIN",
]
