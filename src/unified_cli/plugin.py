"""Stable, dependency-free provider plugin ABI.

Third-party distributions expose one :class:`ProviderPluginV1` instance from
each ``unified_cli.providers.v1`` entry point.  The entry-point name must be
the same as the plugin's ``id``.  Discovery and loading live in
``unified_cli.registry`` so importing this module never scans installed
packages.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, FrozenSet, Iterable, Literal, Tuple

from .base import BaseProvider
from .core import ModelInfo, ProviderId


PROVIDER_PLUGIN_ABI_V1 = 1

ProviderFactoryV1 = Callable[..., BaseProvider]
ProviderModelListerV1 = Callable[[], Iterable[ModelInfo]]
ProviderDoctorV1 = Callable[[], Any]
ProviderSupportStatusV1 = Literal[
    "stable", "preview", "experimental", "held",
]

_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_CORE_ROUTING_PREFIXES = (
    "claude-", "gpt-", "o1-", "o3-", "codex-", "gemini-",
)
_MAX_MODEL_ID_CHARS = 512
_PROVIDER_SUPPORT_STATUSES = frozenset({
    "stable", "preview", "experimental", "held",
})


def _valid_provider_id(value: object) -> bool:
    return (
        # Entry-point metadata comes from another distribution. Reject
        # ``str`` subclasses so custom ``__len__``/comparison methods are not
        # evaluated outside the registry's normalization boundary.
        type(value) is str
        and len(value) <= 64
        and _ID_RE.fullmatch(value) is not None
        and not value.startswith(_CORE_ROUTING_PREFIXES)
    )


def _valid_model_id(value: object) -> bool:
    if type(value) is not str or not value or len(value) > _MAX_MODEL_ID_CHARS:
        return False
    if value != value.strip():
        return False
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return False
    return not any(
        unicodedata.category(char).startswith("C")
        or unicodedata.category(char) in {"Zl", "Zp"}
        for char in value
    )


@dataclass(frozen=True)
class ProviderServerPolicyV1:
    """A plugin's declared HTTP-server posture.

    ``enabled`` defaults to false.  It is descriptive metadata, never an
    authorization grant: unified-cli v1's server rejects every extension
    provider even when a plugin sets it to true.
    """

    enabled: bool = False
    requires_external_isolation: bool = True

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("server policy 'enabled' must be bool")
        if type(self.requires_external_isolation) is not bool:
            raise TypeError(
                "server policy 'requires_external_isolation' must be bool"
            )


@dataclass(frozen=True)
class ProviderPluginV1:
    """Immutable provider implementation metadata for ABI version 1.

    ABI v1 deliberately permits only the exact ``id/model`` route.  The
    ``route_prefixes`` field is retained in the versioned metadata shape for
    forward compatibility, but it is normalized to ``(id,)`` and cannot add
    aliases or extension model-name inference.
    """

    id: ProviderId
    factory: ProviderFactoryV1
    default_model: str
    model_lister: ProviderModelListerV1
    doctor: ProviderDoctorV1
    capabilities: FrozenSet[str] = field(default_factory=frozenset)
    route_prefixes: Tuple[str, ...] = field(default_factory=tuple)
    server_policy: ProviderServerPolicyV1 = field(
        default_factory=ProviderServerPolicyV1
    )
    abi_version: int = PROVIDER_PLUGIN_ABI_V1
    # Appended with a conservative default so existing positional and keyword
    # call sites remain source-compatible without implying release-level
    # compatibility evidence. Runtime availability belongs to the registry.
    support_status: ProviderSupportStatusV1 = "experimental"

    def __post_init__(self) -> None:
        if self.abi_version != PROVIDER_PLUGIN_ABI_V1:
            raise ValueError("unsupported provider plugin ABI")
        if not _valid_provider_id(self.id):
            raise ValueError("invalid provider plugin id")
        if not callable(self.factory):
            raise TypeError("provider factory must be callable")
        if not _valid_model_id(self.default_model):
            raise ValueError("provider default_model must be a non-empty string")
        if not callable(self.model_lister):
            raise TypeError("provider model_lister must be callable")
        if not callable(self.doctor):
            raise TypeError("provider doctor must be callable")
        if (
            type(self.support_status) is not str
            or self.support_status not in _PROVIDER_SUPPORT_STATUSES
        ):
            raise ValueError("invalid provider support status")

        capabilities = self.capabilities
        if isinstance(capabilities, str):
            raise TypeError("provider capabilities must be an iterable of names")
        try:
            frozen_capabilities = frozenset(capabilities)
        except TypeError as exc:
            raise TypeError("provider capabilities must be an iterable of names") from exc
        if any(
            type(item) is not str
            or len(item) > 64
            or _CAPABILITY_RE.fullmatch(item) is None
            for item in frozen_capabilities
        ):
            raise ValueError("invalid provider capability name")
        if self.support_status == "held" and frozen_capabilities:
            raise ValueError("held provider plugins cannot advertise capabilities")
        object.__setattr__(self, "capabilities", frozen_capabilities)

        prefixes = self.route_prefixes or (self.id,)
        if isinstance(prefixes, str):
            raise TypeError("provider route_prefixes must be an iterable of ids")
        try:
            frozen_prefixes = tuple(prefixes)
        except TypeError as exc:
            raise TypeError("provider route_prefixes must be an iterable of ids") from exc
        if len(set(frozen_prefixes)) != len(frozen_prefixes):
            raise ValueError("provider route_prefixes must be unique")
        if any(not _valid_provider_id(prefix) for prefix in frozen_prefixes):
            raise ValueError("invalid provider route prefix")
        if frozen_prefixes != (self.id,):
            raise ValueError(
                "provider ABI v1 route_prefixes must contain only the plugin id"
            )
        object.__setattr__(self, "route_prefixes", frozen_prefixes)

        if not isinstance(self.server_policy, ProviderServerPolicyV1):
            raise TypeError("provider server_policy must be ProviderServerPolicyV1")


__all__ = [
    "PROVIDER_PLUGIN_ABI_V1",
    "ProviderDoctorV1",
    "ProviderFactoryV1",
    "ProviderModelListerV1",
    "ProviderPluginV1",
    "ProviderServerPolicyV1",
    "ProviderSupportStatusV1",
]
