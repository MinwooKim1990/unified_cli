"""Preview ACP adapter for the official Hermes Agent CLI."""

from __future__ import annotations

from types import MappingProxyType

from .acp_bridge import acp_plugin
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


# Official sources:
# https://github.com/NousResearch/hermes-agent/blob/main/pyproject.toml
# https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/cli-commands.md
# https://github.com/NousResearch/hermes-agent/blob/main/acp_adapter/entry.py
# https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/environment-variables.md
HERMES_OFFICIAL_PACKAGE = "hermes-agent[acp]"
HERMES_STAGE_6_VERSION = "0.19.0"
HERMES_ACP_FIXED_ARGV = ("acp",)
_PROBE_LIMITS = OperationLimits(15.0, 256 * 1024, 64 * 1024, 8)
_PROMPT_LIMITS = OperationLimits(120.0, 16 * 1024 * 1024, 1024 * 1024, 50_000)
_FIXED_ENV = MappingProxyType(
    {
        # Hermes documents both switches for isolated integrations.  The
        # provider HOME remains private and may still contain its own auth.
        "HERMES_IGNORE_RULES": "1",
        "HERMES_IGNORE_USER_CONFIG": "1",
    }
)


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(argv, limits=_PROBE_LIMITS)


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="hermes",
    display_name="Hermes Agent",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable="hermes",
        expected_identity="hermes",
        version_probe=VersionProbeSpec(
            _command("--version"),
            minimum_version=(0, 19, 0),
            format=ProbeFormat.PLAIN_TEXT,
            version_marker="Hermes Agent v",
            identity_marker="Hermes Agent v",
            version_is_first_token=True,
            identity_prefix=True,
        ),
        feature_probe=FeatureProbeSpec(
            _command("acp", "--help"),
            required_features=frozenset(("acp", "chat", "check", "version")),
            format=ProbeFormat.PLAIN_TEXT,
            feature_markers={
                "acp": "usage: hermes acp",
                "chat": "Start Hermes Agent in ACP mode",
                "check": "--check",
                "version": "--version",
            },
            identity_marker="usage: hermes acp",
            marker_prefixes=True,
            identity_prefix=True,
        ),
    ),
    prompt=PromptCommandSpec(
        fixed_argv=HERMES_ACP_FIXED_ARGV,
        mode=PromptMode.PROTOCOL,
        prompt_option=None,
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.ACP,
    environment=EnvironmentPolicy(fixed_values=_FIXED_ENV),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("acp", "--check"))),
    capabilities=frozenset((ProviderCapability.CHAT.value,)),
    server_policy=AdapterServerPolicy(enabled=False),
)

PLUGIN = acp_plugin(
    ADAPTER_SPEC,
    launch_resolver=path_launch_resolver(
        provider_id=ADAPTER_SPEC.id,
        executable=ADAPTER_SPEC.binary.executable,
        package_names=("hermes-agent",),
    ),
)


__all__ = [
    "ADAPTER_SPEC",
    "HERMES_ACP_FIXED_ARGV",
    "HERMES_OFFICIAL_PACKAGE",
    "HERMES_STAGE_6_VERSION",
    "PLUGIN",
]
