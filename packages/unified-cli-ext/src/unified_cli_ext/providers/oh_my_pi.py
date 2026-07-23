"""Opt-in Preview bridge for Oh My Pi's documented RPC mode."""

from __future__ import annotations

from .contract import (
    AdapterServerPolicy,
    AdapterStatus,
    BinarySpec,
    DoctorProbeSpec,
    EnvironmentPolicy,
    ExitStatusProbeSpec,
    FeatureProbeSpec,
    ProbeFormat,
    PromptCommandSpec,
    PromptMode,
    ProviderAdapterSpecV1,
    ProviderCapability,
    TransportKind,
    VersionProbeSpec,
)
from .pi import (
    _PROMPT_LIMITS,
    _PiRpcBridge,
    _command,
    _protocol_plugin,
)
from .path_resolver import path_launch_resolver


OH_MY_PI_OFFICIAL_PACKAGE = "@oh-my-pi/pi-coding-agent"
OH_MY_PI_DEFAULT_MODEL = "provider-default"
OH_MY_PI_PROMPT_ID = "unified-cli-ext-turn"
OH_MY_PI_RPC_FIXED_ARGV = (
    "--mode",
    "rpc",
    "--no-session",
    "--no-tools",
    "--no-extensions",
    "--no-skills",
    "--no-rules",
    "--no-lsp",
    "--no-pty",
    "--no-prewalk",
    "--no-title",
    "--approval-mode",
    "always-ask",
)


class _OhMyPiRpcBridge(_PiRpcBridge):
    _provider_label = "Oh My Pi"
    _prompt_id = OH_MY_PI_PROMPT_ID
    _requires_ready = True
    _terminal_event = "agent_end"


ADAPTER_SPEC = ProviderAdapterSpecV1(
    id="oh-my-pi",
    display_name="Oh My Pi",
    status=AdapterStatus.PREVIEW,
    binary=BinarySpec(
        executable="omp",
        expected_identity="omp",
        version_probe=VersionProbeSpec(
            _command("--version"),
            minimum_version=(0,),
            format=ProbeFormat.PLAIN_TEXT,
            version_marker="omp/",
            identity_marker="omp/",
            identity_prefix=True,
        ),
        feature_probe=FeatureProbeSpec(
            _command("--help"),
            required_features=frozenset(("chat", "stream")),
            format=ProbeFormat.PLAIN_TEXT,
            feature_markers={
                "chat": "--mode=<value>",
                "stream": "--no-tools",
            },
            identity_marker="omp v",
            marker_prefixes=True,
            identity_prefix=True,
        ),
    ),
    prompt=PromptCommandSpec(
        fixed_argv=OH_MY_PI_RPC_FIXED_ARGV,
        mode=PromptMode.PROTOCOL,
        limits=_PROMPT_LIMITS,
    ),
    transport=TransportKind.JSONL,
    environment=EnvironmentPolicy(),
    doctor=DoctorProbeSpec(ExitStatusProbeSpec(_command("--version"))),
    capabilities=frozenset(
        (ProviderCapability.CHAT.value, ProviderCapability.STREAM.value)
    ),
    server_policy=AdapterServerPolicy(enabled=False),
)


PLUGIN = _protocol_plugin(
    ADAPTER_SPEC,
    default_model=OH_MY_PI_DEFAULT_MODEL,
    launch_resolver=path_launch_resolver(
        provider_id="oh-my-pi",
        executable="omp",
        package_names=(OH_MY_PI_OFFICIAL_PACKAGE,),
    ),
    bridge_type=_OhMyPiRpcBridge,
)


__all__ = [
    "ADAPTER_SPEC",
    "OH_MY_PI_DEFAULT_MODEL",
    "OH_MY_PI_OFFICIAL_PACKAGE",
    "OH_MY_PI_PROMPT_ID",
    "OH_MY_PI_RPC_FIXED_ARGV",
    "PLUGIN",
]
