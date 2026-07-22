"""Stable, dependency-free provider plugin ABI.

Third-party distributions expose one :class:`ProviderPluginV1` instance from
each ``unified_cli.providers.v1`` entry point.  The entry-point name must be
the same as the plugin's ``id``.  Discovery and loading live in
``unified_cli.registry`` so importing this module never scans installed
packages.
"""

from __future__ import annotations

import math
import os
import re
import unicodedata
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    FrozenSet,
    Iterable,
    Literal,
    Mapping,
    Optional,
    Tuple,
)

from .base import BaseProvider
from .core import ModelInfo, ProviderId


PROVIDER_PLUGIN_ABI_V1 = 1
PROVIDER_CONFIGURATION_ABI_V1 = 1

ProviderFactoryV1 = Callable[..., BaseProvider]
ProviderModelListerV1 = Callable[[], Iterable[ModelInfo]]
ProviderDoctorV1 = Callable[[], Any]
ProviderSupportStatusV1 = Literal[
    "stable", "preview", "experimental", "held",
]

_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_MEDIA_TYPE_RE = re.compile(
    r"^[a-z][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
)
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_CORE_ROUTING_PREFIXES = (
    "claude-", "gpt-", "o1-", "o3-", "codex-", "gemini-",
)
_MAX_MODEL_ID_CHARS = 512
_PROVIDER_SUPPORT_STATUSES = frozenset({
    "stable", "preview", "experimental", "held",
})
_MAX_RECEIPT_DEPTH = 32
_MAX_RECEIPT_ITEMS = 8192
_MAX_RECEIPT_TEXT_BYTES = 1024 * 1024


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


def _valid_absolute_path(value: object) -> bool:
    if type(value) is not str or not value or "\x00" in value:
        return False
    try:
        encoded = os.fsencode(value)
    except UnicodeError:
        return False
    return (
        len(encoded) <= 16 * 1024
        and os.path.isabs(value)
        and os.path.normpath(value) == value
    )


def _freeze_receipt_json(value: Any) -> Any:
    """Copy one bounded plain-JSON value into Core-owned immutable containers."""

    budget = [_MAX_RECEIPT_ITEMS, _MAX_RECEIPT_TEXT_BYTES]

    def freeze(item: Any, depth: int) -> Any:
        if depth > _MAX_RECEIPT_DEPTH:
            raise ValueError("provider receipt payload is nested too deeply")
        budget[0] -= 1
        if budget[0] < 0:
            raise ValueError("provider receipt payload has too many values")
        if item is None or type(item) in (bool, int):
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("provider receipt payload number is invalid")
            return item
        if type(item) is str:
            try:
                encoded = item.encode("utf-8", "strict")
            except UnicodeError:
                raise ValueError("provider receipt payload text is invalid") from None
            budget[1] -= len(encoded)
            if budget[1] < 0 or any(
                unicodedata.category(char).startswith("C")
                or unicodedata.category(char) in {"Zl", "Zp"}
                for char in item
            ):
                raise ValueError("provider receipt payload text is invalid")
            return item
        if type(item) in (list, tuple):
            return tuple(freeze(child, depth + 1) for child in item)
        if not isinstance(item, Mapping):
            raise TypeError("provider receipt payload must contain JSON values")
        copied = {}
        for key, child in item.items():
            if type(key) is not str or not key or len(key) > 128:
                raise ValueError("provider receipt payload key is invalid")
            try:
                encoded_key = key.encode("utf-8", "strict")
            except UnicodeError:
                raise ValueError("provider receipt payload key is invalid") from None
            budget[1] -= len(encoded_key)
            if budget[1] < 0 or any(
                unicodedata.category(char).startswith("C")
                or unicodedata.category(char) in {"Zl", "Zp"}
                for char in key
            ):
                raise ValueError("provider receipt payload key is invalid")
            copied[key] = freeze(child, depth + 1)
        return MappingProxyType(copied)

    return freeze(value, 0)


def _copy_provider_env(value: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or len(value) > 64:
        raise TypeError("provider environment must be a bounded mapping")
    copied = {}
    for key, item in value.items():
        if (
            type(key) is not str
            or _ENV_KEY_RE.fullmatch(key) is None
            or type(item) is not str
        ):
            raise ValueError("provider environment entry is invalid")
        try:
            encoded_item = item.encode("utf-8", "strict")
        except UnicodeError:
            raise ValueError("provider environment entry is invalid") from None
        if (
            len(encoded_item) > 64 * 1024
            or "\x00" in item
            or "\n" in item
            or "\r" in item
        ):
            raise ValueError("provider environment entry is invalid")
        copied[key] = item
    return MappingProxyType(copied)


@dataclass(frozen=True)
class ProviderReceiptEnvelopeV1:
    """Core-owned, format-tagged serialized installation evidence."""

    provider_id: ProviderId
    media_type: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not _valid_provider_id(self.provider_id):
            raise ValueError("invalid provider receipt id")
        if (
            type(self.media_type) is not str
            or _MEDIA_TYPE_RE.fullmatch(self.media_type) is None
        ):
            raise ValueError("invalid provider receipt media type")
        if not isinstance(self.payload, Mapping):
            raise TypeError("provider receipt payload must be a mapping")
        object.__setattr__(self, "payload", _freeze_receipt_json(self.payload))


@dataclass(frozen=True)
class ProviderLaunchContextV1:
    """One explicit, non-persistent input snapshot for a provider binder."""

    provider_id: ProviderId
    receipt: Optional[ProviderReceiptEnvelopeV1] = None
    bin_path: Optional[str] = None
    provider_home: Optional[str] = None
    provider_env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _valid_provider_id(self.provider_id):
            raise ValueError("invalid provider launch id")
        if self.receipt is not None:
            if (
                type(self.receipt) is not ProviderReceiptEnvelopeV1
                or self.receipt.provider_id != self.provider_id
            ):
                raise ValueError("provider receipt does not match launch context")
        if self.receipt is not None and self.bin_path is not None:
            raise ValueError("provider receipt and bin_path are mutually exclusive")
        for label, path in (
            ("bin_path", self.bin_path),
            ("provider_home", self.provider_home),
        ):
            if path is None:
                continue
            if not _valid_absolute_path(path):
                raise ValueError("provider {} is invalid".format(label))
        object.__setattr__(self, "provider_env", _copy_provider_env(self.provider_env))


@dataclass(frozen=True)
class ProviderCreateRequestV1:
    """Finite Core constructor inputs accepted by a bound provider factory."""

    provider_id: ProviderId
    model: str
    workspace: str
    timeout: Optional[float] = None
    max_output_bytes: Optional[int] = None
    max_stderr_bytes: Optional[int] = None
    max_stream_buffer_bytes: Optional[int] = None
    max_stream_events: Optional[int] = None
    max_stream_line_bytes: Optional[int] = None

    def __post_init__(self) -> None:
        if not _valid_provider_id(self.provider_id):
            raise ValueError("invalid provider create id")
        if not _valid_model_id(self.model):
            raise ValueError("invalid provider create model")
        if not _valid_absolute_path(self.workspace):
            raise ValueError("provider workspace is invalid")
        if self.timeout is not None:
            if type(self.timeout) not in (int, float):
                raise ValueError("provider timeout is invalid")
            try:
                numeric_timeout = float(self.timeout)
            except (OverflowError, ValueError):
                raise ValueError("provider timeout is invalid") from None
            if not math.isfinite(numeric_timeout) or numeric_timeout <= 0:
                raise ValueError("provider timeout is invalid")
        for value in (
            self.max_output_bytes,
            self.max_stderr_bytes,
            self.max_stream_buffer_bytes,
            self.max_stream_events,
            self.max_stream_line_bytes,
        ):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("provider runtime limit is invalid")


ProviderBoundFactoryV1 = Callable[[ProviderCreateRequestV1], BaseProvider]


@dataclass(frozen=True)
class BoundProviderOperationsV1:
    """Provider callbacks closed over one validated launch configuration."""

    provider_id: ProviderId
    factory: ProviderBoundFactoryV1
    model_lister: ProviderModelListerV1
    doctor: ProviderDoctorV1
    normalized_receipt: Optional[ProviderReceiptEnvelopeV1]
    provider_home: Optional[str]

    def __post_init__(self) -> None:
        if not _valid_provider_id(self.provider_id):
            raise ValueError("invalid bound provider id")
        if (
            not callable(self.factory)
            or not callable(self.model_lister)
            or not callable(self.doctor)
        ):
            raise TypeError("bound provider callbacks must be callable")
        if self.normalized_receipt is not None and (
            type(self.normalized_receipt) is not ProviderReceiptEnvelopeV1
            or self.normalized_receipt.provider_id != self.provider_id
        ):
            raise ValueError("bound provider receipt is invalid")
        if self.provider_home is not None and not _valid_absolute_path(
            self.provider_home
        ):
            raise ValueError("bound provider home is invalid")


ProviderLaunchBinderV1 = Callable[
    [ProviderLaunchContextV1], BoundProviderOperationsV1
]


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
    # Additive configuration sub-ABI. Existing ABI-v1 plugins leave all three
    # fields at their conservative defaults and keep their historical callback
    # behavior unchanged.
    configuration_abi_version: Optional[int] = None
    launch_binder: Optional[ProviderLaunchBinderV1] = None
    environment_keys: FrozenSet[str] = field(default_factory=frozenset)

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

        if isinstance(self.environment_keys, str):
            raise TypeError("provider environment_keys must be an iterable of names")
        try:
            environment_keys = frozenset(self.environment_keys)
        except TypeError as exc:
            raise TypeError("provider environment_keys must be iterable") from exc
        if len(environment_keys) > 64 or any(
            type(item) is not str or _ENV_KEY_RE.fullmatch(item) is None
            for item in environment_keys
        ):
            raise ValueError("invalid provider environment key")
        configured = self.configuration_abi_version is not None
        if configured:
            if (
                type(self.configuration_abi_version) is not int
                or self.configuration_abi_version != PROVIDER_CONFIGURATION_ABI_V1
            ):
                raise ValueError("unsupported provider configuration ABI")
            if not callable(self.launch_binder):
                raise TypeError("configured provider requires a launch binder")
        elif self.launch_binder is not None or environment_keys:
            raise ValueError("provider configuration metadata is incomplete")
        if self.support_status == "held" and configured:
            raise ValueError("held provider plugins cannot advertise configuration")
        object.__setattr__(self, "environment_keys", environment_keys)

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
    "PROVIDER_CONFIGURATION_ABI_V1",
    "PROVIDER_PLUGIN_ABI_V1",
    "BoundProviderOperationsV1",
    "ProviderBoundFactoryV1",
    "ProviderCreateRequestV1",
    "ProviderDoctorV1",
    "ProviderFactoryV1",
    "ProviderLaunchBinderV1",
    "ProviderLaunchContextV1",
    "ProviderModelListerV1",
    "ProviderPluginV1",
    "ProviderReceiptEnvelopeV1",
    "ProviderServerPolicyV1",
    "ProviderSupportStatusV1",
]
