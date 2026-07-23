"""Preview ACP 0.11 adapter for the official Qoder CLI."""

from __future__ import annotations

import os
from functools import partial

from .acp_bridge import acp_plugin, reject_workspace_config, write_private_json
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


QODER_OFFICIAL_PACKAGE = "@qoder-ai/qodercli"
QODER_STAGE_6_VERSION = "1.1.1"
QODER_ACP_FIXED_ARGV = ("--acp", "--permission-mode", "dont_ask")
_PROBE_LIMITS = OperationLimits(10.0, 64 * 1024, 16 * 1024, 8)
_PROMPT_LIMITS = OperationLimits(120.0, 16 * 1024 * 1024, 1024 * 1024, 50_000)


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(argv, limits=_PROBE_LIMITS)


def _prepare_home(home: str) -> None:
    write_private_json(
        os.path.join(home, ".qoder", "settings.json"),
        {
            "general": {
                "enableAutoUpdate": False,
                "defaultPermissionMode": "dont_ask",
            },
            "permissions": {"allow": [], "ask": [], "deny": ["*"]},
        },
    )


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="qoder",
    display_name="Qoder CLI",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable="qodercli",
        expected_identity="qodercli",
        version_probe=VersionProbeSpec(
            _command("--version"),
            minimum_version=(1, 1, 1),
            format=ProbeFormat.PLAIN_TEXT,
            version_marker="qodercli ",
            identity_marker="qodercli 1.1.1",
            version_is_first_token=True,
            identity_prefix=True,
        ),
        feature_probe=FeatureProbeSpec(
            _command("--help"),
            required_features=frozenset(("acp", "chat", "permission")),
            format=ProbeFormat.PLAIN_TEXT,
            feature_markers={
                "acp": "--acp",
                "chat": "Agent Client Protocol",
                "permission": "--permission-mode",
            },
            identity_marker="Usage: qodercli",
            marker_prefixes=True,
            identity_prefix=True,
        ),
    ),
    prompt=PromptCommandSpec(
        fixed_argv=QODER_ACP_FIXED_ARGV,
        mode=PromptMode.PROTOCOL,
        prompt_option=None,
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.ACP,
    environment=EnvironmentPolicy(
        allowed_keys=frozenset(("QODER_PERSONAL_ACCESS_TOKEN",))
    ),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("--version"))),
    capabilities=frozenset((ProviderCapability.CHAT.value,)),
    server_policy=AdapterServerPolicy(enabled=False),
)

PLUGIN = acp_plugin(
    ADAPTER_SPEC,
    launch_resolver=path_launch_resolver(
        provider_id=ADAPTER_SPEC.id,
        executable=ADAPTER_SPEC.binary.executable,
        package_names=(QODER_OFFICIAL_PACKAGE,),
    ),
    home_preparer=_prepare_home,
    workspace_guard=partial(
        reject_workspace_config,
        names=(".qoder", ".mcp.json"),
    ),
)
