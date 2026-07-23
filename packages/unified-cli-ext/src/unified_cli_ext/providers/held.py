"""Inert Core-plugin metadata for adapters awaiting compatibility evidence.

This module intentionally contains no binary resolution, environment reads, or
process helpers.  A held plugin can be imported by Core's explicit entry-point
loader, but cannot construct a provider or start an external command.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import FrozenSet, Mapping, Optional, Tuple

from unified_cli.plugin import ProviderPluginV1, ProviderServerPolicyV1

from ..errors import ConfigurationError
from .contract import (
    AdapterServerPolicy,
    AdapterStatus,
    BinarySpec,
    EnvironmentPolicy,
    FeatureProbeSpec,
    FixedCommandSpec,
    OperationLimits,
    ProbeFormat,
    PromptCommandSpec,
    PromptMode,
    PromptSentinelPolicy,
    ProviderAdapterSpecV1,
    TransportKind,
    VersionProbeSpec,
)


HELD_UNAVAILABLE_MESSAGE = (
    "This provider integration is unavailable pending verified compatibility evidence."
)
"""Stable, generic failure text for every Stage 5B held integration."""

_PROBE_LIMITS = OperationLimits(
    timeout_seconds=10.0,
    max_stdout_bytes=64 * 1024,
    max_stderr_bytes=16 * 1024,
    max_events=2,
)


class HeldProviderUnavailableError(ConfigurationError):
    """Raised before a held factory can create or execute a provider."""


def _command(*argv: str) -> FixedCommandSpec:
    return FixedCommandSpec(argv=argv, limits=_PROBE_LIMITS)


def held_adapter_spec(
    *,
    provider_id: str,
    display_name: str,
    executable: str,
    prompt_argv: Tuple[str, ...],
    prompt_mode: PromptMode,
    prompt_option: Optional[str],
    sentinel_policy: PromptSentinelPolicy = PromptSentinelPolicy.FORBIDDEN,
    transport: TransportKind,
    environment_keys: FrozenSet[str] = frozenset(),
    version_marker: str,
    help_chat_marker: str,
    version_argv: Tuple[str, ...] = ("--version",),
    help_argv: Tuple[str, ...] = ("--help",),
) -> ProviderAdapterSpecV1:
    """Build immutable, execution-disabled adapter metadata.

    The marker fields are deliberately provisional until Stage 6 records
    isolated command output fixtures.  They are never evaluated while the
    adapter remains held.
    """

    return ProviderAdapterSpecV1(
        id=provider_id,
        display_name=display_name,
        status=AdapterStatus.HELD,
        binary=BinarySpec(
            executable=executable,
            expected_identity=provider_id,
            version_probe=VersionProbeSpec(
                command=_command(*version_argv),
                minimum_version=(0,),
                format=ProbeFormat.PLAIN_TEXT,
                version_marker=version_marker,
            ),
            feature_probe=FeatureProbeSpec(
                command=_command(*help_argv),
                required_features=frozenset(("chat",)),
                format=ProbeFormat.PLAIN_TEXT,
                feature_markers={"chat": help_chat_marker},
            ),
        ),
        prompt=PromptCommandSpec(
            fixed_argv=prompt_argv,
            mode=prompt_mode,
            sentinel_policy=sentinel_policy,
            prompt_option=prompt_option,
        ),
        transport=transport,
        environment=EnvironmentPolicy(allowed_keys=environment_keys),
        capabilities=frozenset(("chat",)),
        server_policy=AdapterServerPolicy(enabled=False),
    )


def _held_factory(*args: object, **kwargs: object) -> object:
    """Stop before BaseProvider construction, binary lookup, or execution."""

    del args, kwargs
    raise HeldProviderUnavailableError(HELD_UNAVAILABLE_MESSAGE)


def _empty_models() -> Tuple[()]:
    return ()


def held_doctor(provider_id: str) -> Mapping[str, object]:
    """Return only static, non-sensitive held metadata without a probe."""

    return MappingProxyType(
        {
            "id": provider_id,
            "status": AdapterStatus.HELD.value,
            "available": False,
            "message": HELD_UNAVAILABLE_MESSAGE,
        }
    )


def held_plugin(spec: ProviderAdapterSpecV1) -> ProviderPluginV1:
    """Expose the Core ABI without advertising execution-only capabilities."""

    return ProviderPluginV1(
        id=spec.id,
        factory=_held_factory,
        # Core ABI v1 requires a non-empty default model even though held
        # adapters make no model claim and their lister is always empty.
        default_model="unavailable",
        model_lister=_empty_models,
        doctor=lambda: held_doctor(spec.id),
        capabilities=frozenset(),
        route_prefixes=(spec.id,),
        server_policy=ProviderServerPolicyV1(enabled=False),
        support_status="held",
    )


__all__ = [
    "HELD_UNAVAILABLE_MESSAGE",
    "HeldProviderUnavailableError",
    "held_adapter_spec",
    "held_doctor",
    "held_plugin",
]
