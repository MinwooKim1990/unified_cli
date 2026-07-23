"""Preview ACP 0.11 adapter for the official Poolside Agent CLI."""

from __future__ import annotations

from functools import partial

from .acp_bridge import (
    acp_plugin,
    reject_provider_home_config,
    reject_workspace_config,
)
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


POOLSIDE_STAGE_6_VERSION = "1.0.13"
POOLSIDE_ACP_FIXED_ARGV = ("acp",)
_PROBE_LIMITS = OperationLimits(10.0, 64 * 1024, 16 * 1024, 8)
_PROMPT_LIMITS = OperationLimits(120.0, 16 * 1024 * 1024, 1024 * 1024, 50_000)


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(argv, limits=_PROBE_LIMITS)


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="poolside",
    display_name="Poolside Agent CLI",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable="pool",
        expected_identity="pool",
        version_probe=VersionProbeSpec(
            _command("acp", "--version"),
            minimum_version=(1, 0, 13),
            format=ProbeFormat.PLAIN_TEXT,
            version_marker="pool ",
            identity_marker="pool 1.0.13",
            version_is_first_token=True,
            identity_prefix=True,
        ),
        feature_probe=FeatureProbeSpec(
            _command("acp", "--help"),
            required_features=frozenset(("acp", "chat", "version")),
            format=ProbeFormat.PLAIN_TEXT,
            feature_markers={
                "acp": "Agent Client Protocol",
                "chat": "standard input",
                "version": "--version",
            },
            identity_marker="pool acp",
            marker_prefixes=True,
            identity_prefix=True,
        ),
    ),
    prompt=PromptCommandSpec(
        fixed_argv=POOLSIDE_ACP_FIXED_ARGV,
        mode=PromptMode.PROTOCOL,
        prompt_option=None,
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.ACP,
    environment=EnvironmentPolicy(
        allowed_keys=frozenset(
            (
                "POOLSIDE_API_KEY",
                "POOLSIDE_TOKEN",
                "POOLSIDE_API_URL",
                "POOLSIDE_STANDALONE_BASE_URL",
                "POOLSIDE_STANDALONE_MODEL",
            )
        )
    ),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("acp", "--version"))),
    capabilities=frozenset((ProviderCapability.CHAT.value,)),
    server_policy=AdapterServerPolicy(enabled=False),
)

PLUGIN = acp_plugin(
    ADAPTER_SPEC,
    launch_resolver=path_launch_resolver(
        provider_id=ADAPTER_SPEC.id,
        executable=ADAPTER_SPEC.binary.executable,
    ),
    home_preparer=partial(
        reject_provider_home_config,
        paths=((".config", "poolside", "settings.yaml"),),
    ),
    workspace_guard=partial(reject_workspace_config, names=(".poolside",)),
)
