"""Experimental ACP 0.11 adapter for the official Kilo Code CLI."""

from __future__ import annotations

from functools import partial
from types import MappingProxyType

from .acp_bridge import acp_plugin, reject_workspace_config
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


KILO_OFFICIAL_PACKAGE = "@kilocode/cli"
KILO_STAGE_6_VERSION = "7.4.11"
KILO_ACP_FIXED_ARGV = ("acp", "--hostname", "127.0.0.1", "--port", "0")
_PROBE_LIMITS = OperationLimits(10.0, 64 * 1024, 16 * 1024, 8)
_PROMPT_LIMITS = OperationLimits(120.0, 16 * 1024 * 1024, 1024 * 1024, 50_000)
_FIXED_ENV = MappingProxyType(
    {
        "KILO_PURE": "1",
        "KILO_NO_DAEMON": "1",
        "KILO_CONFIG_CONTENT": (
            '{"mcp":{},"plugin":[],"snapshot":false,'
            '"permission":{"*":"deny"}}'
        ),
    }
)


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(argv, limits=_PROBE_LIMITS)


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="kilo",
    display_name="Kilo Code",
    status=AdapterStatus.EXPERIMENTAL,
    binary=BinarySpec(
        executable="kilo",
        expected_identity="kilo",
        version_probe=VersionProbeSpec(
            _command("--version"),
            minimum_version=(7, 4, 11),
            format=ProbeFormat.PLAIN_TEXT,
            version_marker="kilo ",
            identity_marker="kilo 7.4.11",
            version_is_first_token=True,
            identity_prefix=True,
        ),
        feature_probe=FeatureProbeSpec(
            _command("acp", "--help"),
            required_features=frozenset(("acp", "chat", "loopback", "ephemeral-port")),
            format=ProbeFormat.PLAIN_TEXT,
            feature_markers={
                "acp": "start ACP",
                "chat": "kilo acp",
                "loopback": "--hostname",
                "ephemeral-port": "--port",
            },
            identity_marker="kilo acp",
            marker_prefixes=True,
            identity_prefix=True,
        ),
    ),
    prompt=PromptCommandSpec(
        fixed_argv=KILO_ACP_FIXED_ARGV,
        mode=PromptMode.PROTOCOL,
        prompt_option=None,
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.ACP,
    environment=EnvironmentPolicy(fixed_values=_FIXED_ENV),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("acp", "--version"))),
    capabilities=frozenset((ProviderCapability.CHAT.value,)),
    server_policy=AdapterServerPolicy(enabled=False),
)

PLUGIN = acp_plugin(
    ADAPTER_SPEC,
    workspace_guard=partial(
        reject_workspace_config,
        names=(
            "kilo.json",
            "kilo.jsonc",
            ".kilo",
            ".kilocode",
            "opencode.json",
            "opencode.jsonc",
        ),
    ),
)
