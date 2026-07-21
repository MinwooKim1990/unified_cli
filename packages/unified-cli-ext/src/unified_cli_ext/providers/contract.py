"""Immutable, declarative provider-adapter ABI v1.

The contract intentionally contains no discovery or execution hooks.  An
adapter can be imported and registered using metadata alone; binary resolution,
version checks, authentication checks, and model discovery are explicit runtime
operations implemented in :mod:`unified_cli_ext.providers.runtime`.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, FrozenSet, Iterable, Mapping, Optional, Tuple, Union

from ..errors import ConfigurationError
from ..permissions import PermissionPolicy
from ..transports import TransportLimits


PROVIDER_ADAPTER_ABI_V1 = 1

_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ARGUMENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OPTION_RE = re.compile(r"^(?:-[A-Za-z0-9]|--[a-z0-9][a-z0-9-]{0,126})$")
_CORE_ROUTING_PREFIXES = (
    "claude-",
    "gpt-",
    "o1-",
    "o3-",
    "codex-",
    "gemini-",
)
_RESERVED_PROVIDER_IDS = frozenset(("claude", "codex", "gemini", "agy"))
_BASE_ENV_KEYS = frozenset(
    ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM", "HOME", "TMPDIR")
)
_MAX_ADAPTER_SPEC_UTF8_BYTES = 256 * 1024


def _consume_spec_utf8_budget(
    value: Any,
    remaining: int,
    active: Tuple[int, ...] = (),
    depth: int = 0,
) -> int:
    """Bound aggregate textual metadata, including repeated nested values."""

    if depth > 32:
        raise ConfigurationError("provider adapter metadata is too deeply nested")
    if isinstance(value, str):
        remaining -= len(value.encode("utf-8", "strict"))
        if remaining < 0:
            raise ConfigurationError(
                "provider adapter metadata exceeds 262144 UTF-8 bytes"
            )
        return remaining
    if is_dataclass(value) and not isinstance(value, type):
        marker = id(value)
        if marker in active:
            raise ConfigurationError("provider adapter metadata is recursive")
        nested = active + (marker,)
        for item in fields(value):
            remaining = _consume_spec_utf8_budget(
                getattr(value, item.name), remaining, nested, depth + 1
            )
        return remaining
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in active:
            raise ConfigurationError("provider adapter metadata is recursive")
        nested = active + (marker,)
        for key, item in value.items():
            remaining = _consume_spec_utf8_budget(
                key, remaining, nested, depth + 1
            )
            remaining = _consume_spec_utf8_budget(
                item, remaining, nested, depth + 1
            )
        return remaining
    if isinstance(value, (tuple, list, frozenset)):
        marker = id(value)
        if marker in active:
            raise ConfigurationError("provider adapter metadata is recursive")
        nested = active + (marker,)
        for item in value:
            remaining = _consume_spec_utf8_budget(
                item, remaining, nested, depth + 1
            )
    return remaining


def _safe_text(
    value: object,
    *,
    label: str,
    maximum: int,
    empty: bool = False,
    newlines: bool = False,
) -> str:
    if type(value) is not str or (not empty and not value) or len(value) > maximum:
        raise ConfigurationError("{} is invalid".format(label))
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise ConfigurationError("{} is invalid".format(label)) from None
    for char in value:
        category = unicodedata.category(char)
        if char == "\x00" or category in {"Zl", "Zp"}:
            raise ConfigurationError("{} is invalid".format(label))
        if category.startswith("C") and not (newlines and char in "\n\r\t"):
            raise ConfigurationError("{} is invalid".format(label))
    return value


def valid_provider_id(value: object) -> bool:
    """Return whether *value* is safe for Core ABI v1 extension routing."""

    return (
        type(value) is str
        and len(value) <= 64
        and _PROVIDER_ID_RE.fullmatch(value) is not None
        and value not in _RESERVED_PROVIDER_IDS
        and not value.startswith(_CORE_ROUTING_PREFIXES)
    )


def _provider_id(value: object) -> str:
    if not valid_provider_id(value):
        raise ConfigurationError("invalid provider adapter id")
    return value


def _fixed_strings(
    values: Iterable[object],
    *,
    label: str,
    maximum_items: int,
    maximum_chars: int,
    allow_empty_collection: bool = True,
) -> Tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ConfigurationError("{} must be a string collection".format(label))
    collected = []
    try:
        for index, value in enumerate(values):
            if index >= maximum_items:
                raise ConfigurationError("{} has too many entries".format(label))
            collected.append(
                _safe_text(
                    value,
                    label="{} entry".format(label),
                    maximum=maximum_chars,
                    empty=False,
                    newlines=False,
                )
            )
    except ConfigurationError:
        raise
    except Exception:
        raise ConfigurationError("{} is malformed".format(label)) from None
    if not collected and not allow_empty_collection:
        raise ConfigurationError("{} must not be empty".format(label))
    return tuple(collected)


class AdapterStatus(str, Enum):
    STABLE = "Stable"
    PREVIEW = "Preview"
    EXPERIMENTAL = "Experimental"
    HELD = "Held"


class TransportKind(str, Enum):
    PLAIN = "plain"
    JSON = "json"
    JSONL = "jsonl"
    JSON_RPC = "jsonrpc"
    ACP = "acp"
    HTTP_JSON = "http_json"
    HTTP_SSE = "http_sse"


class PromptMode(str, Enum):
    STDIN = "stdin"
    OPTION_VALUE = "option_value"
    POSITIONAL_AFTER_SENTINEL = "positional_after_sentinel"
    PROTOCOL = "protocol"
    # Stage 2 compatibility name.  It is an alias, not a fifth prompt mode.
    ARGV = "positional_after_sentinel"


class ProbeFormat(str, Enum):
    PLAIN_TEXT = "plain_text"
    JSON = "json"
    JSONL = "jsonl"
    EXIT_STATUS = "exit_status"


class PromptSentinelPolicy(str, Enum):
    REQUIRED = "required"
    FORBIDDEN = "forbidden"


class ProviderCapability(str, Enum):
    AUTH = "auth"
    CHAT = "chat"
    STREAM = "stream"
    MODELS = "models"
    SESSIONS = "sessions"
    TOOLS = "tools"
    PERMISSIONS = "permissions"
    REASONING_SUMMARIES = "reasoning_summaries"
    IMAGES = "images"
    MCP = "mcp"


_KNOWN_CAPABILITIES = frozenset(item.value for item in ProviderCapability)


@dataclass(frozen=True)
class TransportConfig:
    """Kind-specific declarative transport configuration.

    ABI v1 intentionally has no HTTP daemon lifecycle specification.  The
    ``base_url`` field remains readable for source compatibility, but adapter
    metadata rejects it: an externally prestarted endpoint is not execution of
    the provider binary named by this adapter.
    """

    base_url: Optional[str] = None

    def __post_init__(self) -> None:
        if self.base_url is not None:
            _safe_text(
                self.base_url,
                label="transport base URL",
                maximum=16 * 1024,
                empty=False,
                newlines=False,
            )


@dataclass(frozen=True)
class OperationLimits:
    """Hard limits for one explicitly requested provider operation."""

    timeout_seconds: float = 10.0
    max_stdout_bytes: int = 1024 * 1024
    max_stderr_bytes: int = 256 * 1024
    max_events: int = 8

    def __post_init__(self) -> None:
        if type(self.timeout_seconds) not in (int, float):
            raise ConfigurationError("operation timeout must be a finite positive number")
        timeout = float(self.timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 300:
            raise ConfigurationError("operation timeout must be between zero and 300 seconds")
        object.__setattr__(self, "timeout_seconds", timeout)
        for label, value, maximum in (
            ("stdout", self.max_stdout_bytes, 64 * 1024 * 1024),
            ("stderr", self.max_stderr_bytes, 8 * 1024 * 1024),
            ("events", self.max_events, 50_000),
        ):
            if type(value) is not int or value <= 0 or value > maximum:
                raise ConfigurationError("operation {} limit is invalid".format(label))

    def transport_limits(self) -> TransportLimits:
        return TransportLimits(
            max_line_bytes=min(self.max_stdout_bytes, 1024 * 1024),
            max_output_bytes=self.max_stdout_bytes,
            max_stderr_bytes=self.max_stderr_bytes,
            max_events=self.max_events,
            max_body_bytes=self.max_stdout_bytes,
            max_redirects=0,
        )


@dataclass(frozen=True)
class FixedCommandSpec:
    """A provider-owned argument vector suffix; never a shell command string."""

    argv: Tuple[str, ...]
    limits: OperationLimits = field(default_factory=OperationLimits)

    def __post_init__(self) -> None:
        argv = _fixed_strings(
            self.argv,
            label="fixed command argv",
            maximum_items=128,
            maximum_chars=16 * 1024,
            allow_empty_collection=False,
        )
        if not isinstance(self.limits, OperationLimits):
            raise ConfigurationError("command limits must be OperationLimits")
        object.__setattr__(self, "argv", argv)

    def build(self, binary_path: str) -> Tuple[str, ...]:
        binary = _safe_text(
            binary_path,
            label="binary path",
            maximum=16 * 1024,
            empty=False,
            newlines=False,
        )
        return (binary,) + self.argv


@dataclass(frozen=True)
class DynamicArgument:
    """One declared option whose value may be supplied at invocation time."""

    name: str
    flag: str
    required: bool = False

    def __post_init__(self) -> None:
        if type(self.name) is not str or _ARGUMENT_NAME_RE.fullmatch(self.name) is None:
            raise ConfigurationError("dynamic argument name is invalid")
        flag = _safe_text(
            self.flag,
            label="dynamic argument flag",
            maximum=128,
            empty=False,
            newlines=False,
        )
        if _OPTION_RE.fullmatch(flag) is None:
            raise ConfigurationError("dynamic argument flag must be a short or long option")
        if type(self.required) is not bool:
            raise ConfigurationError("dynamic argument required marker must be bool")


@dataclass(frozen=True)
class BuiltPromptInvocation:
    argv: Tuple[str, ...]
    stdin_text: Optional[str]
    protocol_text: Optional[str] = None


@dataclass(frozen=True)
class PromptCommandSpec:
    """Declarative prompt builder with an unambiguous option boundary."""

    fixed_argv: Tuple[str, ...]
    dynamic_arguments: Tuple[DynamicArgument, ...] = ()
    mode: PromptMode = PromptMode.STDIN
    sentinel_policy: PromptSentinelPolicy = PromptSentinelPolicy.FORBIDDEN
    prompt_option: Optional[str] = None
    limits: OperationLimits = field(
        default_factory=lambda: OperationLimits(
            timeout_seconds=120.0,
            max_stdout_bytes=16 * 1024 * 1024,
            max_stderr_bytes=1024 * 1024,
            max_events=50_000,
        )
    )

    def __post_init__(self) -> None:
        fixed = _fixed_strings(
            self.fixed_argv,
            label="prompt fixed argv",
            maximum_items=128,
            maximum_chars=16 * 1024,
            allow_empty_collection=False,
        )
        dynamic_values = []
        try:
            for index, item in enumerate(self.dynamic_arguments):
                if index >= 32:
                    raise ConfigurationError("prompt dynamic arguments are invalid")
                dynamic_values.append(item)
        except Exception:
            raise ConfigurationError("prompt dynamic arguments are malformed") from None
        dynamic = tuple(dynamic_values)
        if any(type(item) is not DynamicArgument for item in dynamic):
            raise ConfigurationError("prompt dynamic arguments are invalid")
        names = tuple(item.name for item in dynamic)
        flags = tuple(item.flag for item in dynamic)
        if len(names) != len(set(names)) or len(flags) != len(set(flags)):
            raise ConfigurationError("prompt dynamic arguments must be unique")
        if type(self.mode) is not PromptMode:
            raise ConfigurationError("prompt mode must be PromptMode")
        if type(self.sentinel_policy) is not PromptSentinelPolicy:
            raise ConfigurationError("prompt sentinel policy is invalid")
        if self.mode is PromptMode.POSITIONAL_AFTER_SENTINEL:
            if self.sentinel_policy is not PromptSentinelPolicy.REQUIRED:
                raise ConfigurationError("positional prompts require the '--' sentinel")
        elif self.sentinel_policy is not PromptSentinelPolicy.FORBIDDEN:
            raise ConfigurationError("this prompt mode must not add an argv sentinel")
        if self.mode is PromptMode.OPTION_VALUE:
            option = _safe_text(
                self.prompt_option,
                label="prompt option",
                maximum=128,
                empty=False,
                newlines=False,
            )
            if _OPTION_RE.fullmatch(option) is None:
                raise ConfigurationError("prompt option must be a short or long option")
            if option in flags:
                raise ConfigurationError("prompt option must not duplicate a dynamic option")
            object.__setattr__(self, "prompt_option", option)
        elif self.prompt_option is not None:
            raise ConfigurationError("prompt option is valid only for option-value prompts")
        if not isinstance(self.limits, OperationLimits):
            raise ConfigurationError("prompt limits must be OperationLimits")
        object.__setattr__(self, "fixed_argv", fixed)
        object.__setattr__(self, "dynamic_arguments", dynamic)

    def build(
        self,
        binary_path: str,
        prompt: str,
        values: Optional[Mapping[str, str]] = None,
    ) -> BuiltPromptInvocation:
        binary = _safe_text(
            binary_path,
            label="binary path",
            maximum=16 * 1024,
            empty=False,
            newlines=False,
        )
        clean_prompt = _safe_text(
            prompt,
            label="prompt",
            maximum=16 * 1024 * 1024,
            empty=True,
            newlines=True,
        )
        source = values if values is not None else {}
        if not isinstance(source, Mapping):
            raise ConfigurationError("prompt option values must be a mapping")
        allowed = frozenset(item.name for item in self.dynamic_arguments)
        supplied_values = []
        try:
            for index, name in enumerate(source.keys()):
                if index >= 33:
                    raise ConfigurationError("prompt option values contain too many entries")
                supplied_values.append(name)
        except Exception:
            raise ConfigurationError("prompt option values are malformed") from None
        supplied = tuple(supplied_values)
        if any(type(name) is not str for name in supplied) or not set(supplied) <= allowed:
            raise ConfigurationError("prompt option value is not declared")
        argv = [binary]
        argv.extend(self.fixed_argv)
        for argument in self.dynamic_arguments:
            try:
                present = argument.name in source
                value = source[argument.name] if present else None
            except Exception:
                raise ConfigurationError("prompt option values are malformed") from None
            if not present:
                if argument.required:
                    raise ConfigurationError("required prompt option is missing")
                continue
            clean = _safe_text(
                value,
                label="prompt option value",
                maximum=16 * 1024,
                empty=False,
                newlines=False,
            )
            argv.extend((argument.flag, clean))
        if self.mode is PromptMode.POSITIONAL_AFTER_SENTINEL:
            argv.extend(("--", clean_prompt))
            return BuiltPromptInvocation(tuple(argv), None, None)
        if self.mode is PromptMode.OPTION_VALUE:
            argv.extend((self.prompt_option, clean_prompt))
            return BuiltPromptInvocation(tuple(argv), None, None)
        if self.mode is PromptMode.PROTOCOL:
            return BuiltPromptInvocation(tuple(argv), None, clean_prompt)
        return BuiltPromptInvocation(tuple(argv), clean_prompt, None)


@dataclass(frozen=True)
class EnvironmentPolicy:
    """Provider-specific credential broker allowlist.

    Ambient variables are never copied here.  ``select`` reads only declared
    keys, so passing a larger mapping cannot accidentally forward unrelated
    credentials.
    """

    allowed_keys: FrozenSet[str] = frozenset()
    required_keys: FrozenSet[str] = frozenset()

    def __post_init__(self) -> None:
        allowed = self._keys(self.allowed_keys, "environment allowlist")
        required = self._keys(self.required_keys, "required environment keys")
        if not required <= allowed:
            raise ConfigurationError("required environment keys must be allowlisted")
        object.__setattr__(self, "allowed_keys", allowed)
        object.__setattr__(self, "required_keys", required)

    @staticmethod
    def _keys(values: Iterable[object], label: str) -> FrozenSet[str]:
        keys = _fixed_strings(
            values,
            label=label,
            maximum_items=64,
            maximum_chars=128,
        )
        if len(keys) != len(set(keys)):
            raise ConfigurationError("{} contains duplicate keys".format(label))
        if any(_ENV_KEY_RE.fullmatch(key) is None or key in _BASE_ENV_KEYS for key in keys):
            raise ConfigurationError("{} contains an invalid key".format(label))
        return frozenset(keys)

    def select(self, source: Optional[Mapping[str, str]]) -> Mapping[str, str]:
        if source is None:
            source = {}
        if not isinstance(source, Mapping):
            raise ConfigurationError("provider environment must be a mapping")
        selected = {}
        try:
            for key in self.allowed_keys:
                if key not in source:
                    continue
                value = source[key]
                if type(value) is not str:
                    raise ConfigurationError("provider environment value is invalid")
                _safe_text(
                    value,
                    label="provider environment value",
                    maximum=64 * 1024,
                    empty=True,
                    newlines=False,
                )
                selected[key] = value
        except ConfigurationError:
            raise
        except Exception:
            raise ConfigurationError("provider environment is malformed") from None
        if not self.required_keys <= set(selected):
            raise ConfigurationError("required provider environment is unavailable")
        return MappingProxyType(selected)


def _expected_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError("probe expected values must be a mapping")
    clean = {}
    try:
        for index, pair in enumerate(value.items()):
            if index >= 32:
                raise ConfigurationError("probe expected values have too many entries")
            key, item = pair
            key = _safe_text(
                key,
                label="probe expected key",
                maximum=128,
                empty=False,
                newlines=False,
            )
            if type(item) not in (str, bool, int) and item is not None:
                raise ConfigurationError("probe expected value is invalid")
            if type(item) is str:
                _safe_text(
                    item,
                    label="probe expected value",
                    maximum=1024,
                    empty=True,
                    newlines=False,
                )
            if key in clean:
                raise ConfigurationError("probe expected keys must be unique")
            clean[key] = item
    except ConfigurationError:
        raise
    except Exception:
        raise ConfigurationError("probe expected values are malformed") from None
    return MappingProxyType(clean)


@dataclass(frozen=True)
class PlainTextFieldSpec:
    """Extract one bounded field after a literal marker.

    Extraction ends at the first literal terminator (a newline by default),
    and performs no regular-expression or callable parsing.
    """

    marker: str
    terminator: Optional[str] = "\n"
    max_chars: int = 1024
    presence_only: bool = False

    def __post_init__(self) -> None:
        marker = _safe_text(
            self.marker,
            label="plain-text field marker",
            maximum=1024,
            empty=False,
            newlines=False,
        )
        if self.terminator is not None:
            terminator = _safe_text(
                self.terminator,
                label="plain-text field terminator",
                maximum=128,
                empty=False,
                newlines=True,
            )
            object.__setattr__(self, "terminator", terminator)
        if type(self.max_chars) is not int or not 1 <= self.max_chars <= 16 * 1024:
            raise ConfigurationError("plain-text field limit is invalid")
        if type(self.presence_only) is not bool:
            raise ConfigurationError("plain-text presence marker is invalid")
        object.__setattr__(self, "marker", marker)


def _plain_fields(value: Mapping[str, PlainTextFieldSpec]) -> Mapping[str, PlainTextFieldSpec]:
    if not isinstance(value, Mapping):
        raise ConfigurationError("plain-text probe fields must be a mapping")
    clean = {}
    try:
        for index, pair in enumerate(value.items()):
            if index >= 32:
                raise ConfigurationError("plain-text probe fields have too many entries")
            key, item = pair
            key = _safe_text(
                key,
                label="plain-text probe field",
                maximum=128,
                empty=False,
                newlines=False,
            )
            if type(item) is not PlainTextFieldSpec or key in clean:
                raise ConfigurationError("plain-text probe fields are invalid")
            clean[key] = item
    except ConfigurationError:
        raise
    except Exception:
        raise ConfigurationError("plain-text probe fields are malformed") from None
    return MappingProxyType(clean)


@dataclass(frozen=True)
class JsonProbeSpec:
    command: FixedCommandSpec
    expected: Mapping[str, Any] = field(default_factory=dict)
    identity_field: str = "provider"
    format: ProbeFormat = ProbeFormat.JSONL

    def __post_init__(self) -> None:
        if not isinstance(self.command, FixedCommandSpec):
            raise ConfigurationError("probe command must be FixedCommandSpec")
        _safe_text(
            self.identity_field,
            label="probe identity field",
            maximum=128,
            empty=False,
            newlines=False,
        )
        if type(self.format) is not ProbeFormat or self.format not in (
            ProbeFormat.JSON,
            ProbeFormat.JSONL,
        ):
            raise ConfigurationError("JSON probe format must be JSON or JSONL")
        object.__setattr__(self, "expected", _expected_mapping(self.expected))


@dataclass(frozen=True)
class PlainTextProbeSpec:
    command: FixedCommandSpec
    required_markers: Tuple[str, ...] = ()
    fields: Mapping[str, PlainTextFieldSpec] = field(default_factory=dict)
    expected: Mapping[str, Any] = field(default_factory=dict)
    identity_marker: Optional[str] = None
    format: ProbeFormat = field(default=ProbeFormat.PLAIN_TEXT, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.command, FixedCommandSpec):
            raise ConfigurationError("probe command must be FixedCommandSpec")
        markers = _fixed_strings(
            self.required_markers,
            label="plain-text probe markers",
            maximum_items=64,
            maximum_chars=1024,
        )
        if len(markers) != len(set(markers)):
            raise ConfigurationError("plain-text probe markers must be unique")
        if self.identity_marker is not None:
            identity = _safe_text(
                self.identity_marker,
                label="plain-text identity marker",
                maximum=1024,
                empty=False,
                newlines=False,
            )
            object.__setattr__(self, "identity_marker", identity)
        object.__setattr__(self, "required_markers", markers)
        object.__setattr__(self, "fields", _plain_fields(self.fields))
        object.__setattr__(self, "expected", _expected_mapping(self.expected))


@dataclass(frozen=True)
class ExitStatusProbeSpec:
    command: FixedCommandSpec
    expected_status: int = 0
    format: ProbeFormat = field(default=ProbeFormat.EXIT_STATUS, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.command, FixedCommandSpec):
            raise ConfigurationError("probe command must be FixedCommandSpec")
        if type(self.expected_status) is not int or not 0 <= self.expected_status <= 255:
            raise ConfigurationError("expected probe exit status is invalid")


DeclarativeProbeSpec = Union[JsonProbeSpec, PlainTextProbeSpec, ExitStatusProbeSpec]


@dataclass(frozen=True)
class VersionProbeSpec:
    command: FixedCommandSpec
    version_field: str = "version"
    identity_field: str = "provider"
    minimum_version: Tuple[int, ...] = (0,)
    format: ProbeFormat = ProbeFormat.JSONL
    version_marker: Optional[str] = None
    identity_marker: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.command, FixedCommandSpec):
            raise ConfigurationError("version probe command must be FixedCommandSpec")
        if type(self.format) is not ProbeFormat or self.format is ProbeFormat.EXIT_STATUS:
            raise ConfigurationError("version probe format is invalid")
        for field_name in (self.version_field, self.identity_field):
            _safe_text(
                field_name,
                label="version probe field",
                maximum=128,
                empty=False,
                newlines=False,
            )
        minimum_values = []
        try:
            for index, item in enumerate(self.minimum_version):
                if index >= 4:
                    raise ConfigurationError("minimum version is invalid")
                minimum_values.append(item)
        except Exception:
            raise ConfigurationError("minimum version is malformed") from None
        minimum = tuple(minimum_values)
        if (
            not 1 <= len(minimum) <= 4
            or any(type(item) is not int or item < 0 or item > 1_000_000 for item in minimum)
        ):
            raise ConfigurationError("minimum version is invalid")
        if self.format is ProbeFormat.PLAIN_TEXT:
            marker = _safe_text(
                self.version_marker,
                label="plain version marker",
                maximum=1024,
                empty=False,
                newlines=False,
            )
            object.__setattr__(self, "version_marker", marker)
            if self.identity_marker is not None:
                identity = _safe_text(
                    self.identity_marker,
                    label="plain version identity marker",
                    maximum=1024,
                    empty=False,
                    newlines=False,
                )
                object.__setattr__(self, "identity_marker", identity)
        elif self.version_marker is not None or self.identity_marker is not None:
            raise ConfigurationError("plain version markers require plain-text format")
        object.__setattr__(self, "minimum_version", minimum)


@dataclass(frozen=True)
class FeatureProbeSpec:
    command: FixedCommandSpec
    required_features: FrozenSet[str] = frozenset()
    features_field: str = "features"
    identity_field: str = "provider"
    format: ProbeFormat = ProbeFormat.JSONL
    feature_markers: Mapping[str, str] = field(default_factory=dict)
    identity_marker: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.command, FixedCommandSpec):
            raise ConfigurationError("feature probe command must be FixedCommandSpec")
        if type(self.format) is not ProbeFormat:
            raise ConfigurationError("feature probe format is invalid")
        if self.format is ProbeFormat.EXIT_STATUS:
            raise ConfigurationError(
                "exit-status probes cannot provide provider feature evidence"
            )
        features = _fixed_strings(
            self.required_features,
            label="required feature set",
            maximum_items=128,
            maximum_chars=64,
        )
        if len(features) != len(set(features)) or any(
            _CAPABILITY_RE.fullmatch(item) is None for item in features
        ):
            raise ConfigurationError("required feature set is invalid")
        for field_name in (self.features_field, self.identity_field):
            _safe_text(
                field_name,
                label="feature probe field",
                maximum=128,
                empty=False,
                newlines=False,
            )
        markers = {}
        try:
            for index, pair in enumerate(self.feature_markers.items()):
                if index >= 128:
                    raise ConfigurationError("feature probe markers have too many entries")
                name, marker = pair
                if type(name) is not str or _CAPABILITY_RE.fullmatch(name) is None:
                    raise ConfigurationError("feature probe marker name is invalid")
                marker = _safe_text(
                    marker,
                    label="feature probe marker",
                    maximum=1024,
                    empty=False,
                    newlines=False,
                )
                if name in markers:
                    raise ConfigurationError("feature probe markers must be unique")
                markers[name] = marker
        except ConfigurationError:
            raise
        except Exception:
            raise ConfigurationError("feature probe markers are malformed") from None
        if self.format is ProbeFormat.PLAIN_TEXT:
            if not frozenset(features) <= set(markers):
                raise ConfigurationError("plain feature probe must map every required feature")
            if self.identity_marker is not None:
                identity = _safe_text(
                    self.identity_marker,
                    label="plain feature identity marker",
                    maximum=1024,
                    empty=False,
                    newlines=False,
                )
                object.__setattr__(self, "identity_marker", identity)
        elif markers or self.identity_marker is not None:
            raise ConfigurationError("feature markers require plain-text format")
        object.__setattr__(self, "required_features", frozenset(features))
        object.__setattr__(self, "feature_markers", MappingProxyType(markers))


@dataclass(frozen=True)
class DoctorProbeSpec:
    probe: DeclarativeProbeSpec
    healthy_field: str = "ok"

    def __post_init__(self) -> None:
        if not isinstance(
            self.probe, (JsonProbeSpec, PlainTextProbeSpec, ExitStatusProbeSpec)
        ):
            raise ConfigurationError("doctor probe must be declarative")
        _safe_text(
            self.healthy_field,
            label="doctor healthy field",
            maximum=128,
            empty=False,
            newlines=False,
        )


@dataclass(frozen=True)
class ModelProbeSpec:
    probe: JsonProbeSpec
    models_field: str = "models"
    max_models: int = 1000

    def __post_init__(self) -> None:
        if not isinstance(self.probe, JsonProbeSpec):
            raise ConfigurationError("model probe must return JSON output")
        _safe_text(
            self.models_field,
            label="model probe field",
            maximum=128,
            empty=False,
            newlines=False,
        )
        if type(self.max_models) is not int or not 1 <= self.max_models <= 10_000:
            raise ConfigurationError("model probe limit is invalid")


@dataclass(frozen=True)
class AuthSpec:
    status_probe: DeclarativeProbeSpec
    login_command: FixedCommandSpec
    logout_command: Optional[FixedCommandSpec] = None
    authenticated_field: str = "authenticated"

    def __post_init__(self) -> None:
        if not isinstance(
            self.status_probe, (JsonProbeSpec, PlainTextProbeSpec, ExitStatusProbeSpec)
        ):
            raise ConfigurationError("auth status probe must be declarative")
        if not isinstance(self.login_command, FixedCommandSpec):
            raise ConfigurationError("auth login command must be FixedCommandSpec")
        if self.logout_command is not None and not isinstance(
            self.logout_command, FixedCommandSpec
        ):
            raise ConfigurationError("auth logout command must be FixedCommandSpec")
        _safe_text(
            self.authenticated_field,
            label="auth status field",
            maximum=128,
            empty=False,
            newlines=False,
        )


@dataclass(frozen=True)
class BinarySpec:
    executable: str
    expected_identity: str
    version_probe: VersionProbeSpec
    feature_probe: FeatureProbeSpec

    def __post_init__(self) -> None:
        executable = _safe_text(
            self.executable,
            label="binary executable name",
            maximum=255,
            empty=False,
            newlines=False,
        )
        if "/" in executable or "\\" in executable or executable in {".", ".."}:
            raise ConfigurationError("binary executable must be an exact basename")
        identity = _safe_text(
            self.expected_identity,
            label="binary identity",
            maximum=128,
            empty=False,
            newlines=False,
        )
        if not isinstance(self.version_probe, VersionProbeSpec):
            raise ConfigurationError("binary version probe must be VersionProbeSpec")
        if not isinstance(self.feature_probe, FeatureProbeSpec):
            raise ConfigurationError("binary feature probe must be FeatureProbeSpec")
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "expected_identity", identity)


@dataclass(frozen=True)
class AdapterServerPolicy:
    """Ext ABI v1 server posture.  Enabling is deliberately unsupported."""

    enabled: bool = False
    requires_external_isolation: bool = True

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or type(self.requires_external_isolation) is not bool:
            raise ConfigurationError("adapter server policy fields must be bool")
        if self.enabled:
            raise ConfigurationError("provider adapters are disabled in server mode")


@dataclass(frozen=True)
class ProviderAdapterSpecV1:
    id: str
    display_name: str
    status: AdapterStatus
    binary: BinarySpec
    prompt: PromptCommandSpec
    transport: TransportKind
    transport_config: TransportConfig = field(default_factory=TransportConfig)
    environment: EnvironmentPolicy = field(default_factory=EnvironmentPolicy)
    auth: Optional[AuthSpec] = None
    doctor: Optional[DoctorProbeSpec] = None
    models: Optional[ModelProbeSpec] = None
    capabilities: FrozenSet[str] = frozenset()
    route_prefix: Optional[str] = None
    session_namespace: Optional[str] = None
    permission_policy: Optional[PermissionPolicy] = None
    server_policy: AdapterServerPolicy = field(default_factory=AdapterServerPolicy)
    abi_version: int = PROVIDER_ADAPTER_ABI_V1

    def __post_init__(self) -> None:
        provider_id = _provider_id(self.id)
        display_name = _safe_text(
            self.display_name,
            label="provider display name",
            maximum=128,
            empty=False,
            newlines=False,
        )
        if type(self.status) is not AdapterStatus:
            raise ConfigurationError("adapter status must be AdapterStatus")
        if not isinstance(self.binary, BinarySpec):
            raise ConfigurationError("adapter binary must be BinarySpec")
        if self.binary.feature_probe.format is ProbeFormat.EXIT_STATUS:
            raise ConfigurationError(
                "exit-status probes cannot provide provider feature evidence"
            )
        if not isinstance(self.prompt, PromptCommandSpec):
            raise ConfigurationError("adapter prompt must be PromptCommandSpec")
        if type(self.transport) is not TransportKind:
            raise ConfigurationError("adapter transport must be TransportKind")
        if not isinstance(self.transport_config, TransportConfig):
            raise ConfigurationError("adapter transport config must be TransportConfig")
        if self.transport_config.base_url is not None:
            raise ConfigurationError(
                "prestarted HTTP endpoints are unsupported by provider adapter ABI v1"
            )
        process_prompt_transports = {
            TransportKind.PLAIN,
            TransportKind.JSON,
            TransportKind.JSONL,
            TransportKind.JSON_RPC,
        }
        protocol_prompt_transports = {
            TransportKind.JSONL,
            TransportKind.JSON_RPC,
            TransportKind.ACP,
            TransportKind.HTTP_JSON,
            TransportKind.HTTP_SSE,
        }
        if (
            self.prompt.mode is PromptMode.PROTOCOL
            and self.transport not in protocol_prompt_transports
        ):
            raise ConfigurationError("protocol prompt mode requires a protocol transport")
        if (
            self.prompt.mode is not PromptMode.PROTOCOL
            and self.transport not in process_prompt_transports
        ):
            raise ConfigurationError("process prompt mode requires a process transport")
        if self.prompt.mode is PromptMode.STDIN and self.transport in {
            TransportKind.JSONL,
            TransportKind.JSON_RPC,
        }:
            raise ConfigurationError(
                "stdin prompt mode would collide with the subprocess protocol channel"
            )
        if not isinstance(self.environment, EnvironmentPolicy):
            raise ConfigurationError("adapter environment must be EnvironmentPolicy")
        if self.auth is not None and not isinstance(self.auth, AuthSpec):
            raise ConfigurationError("adapter auth must be AuthSpec")
        if self.doctor is not None and not isinstance(self.doctor, DoctorProbeSpec):
            raise ConfigurationError("adapter doctor must be DoctorProbeSpec")
        if self.models is not None and not isinstance(self.models, ModelProbeSpec):
            raise ConfigurationError("adapter models must be ModelProbeSpec")
        capabilities = _fixed_strings(
            self.capabilities,
            label="adapter capabilities",
            maximum_items=64,
            maximum_chars=64,
        )
        if len(capabilities) != len(set(capabilities)) or any(
            item not in _KNOWN_CAPABILITIES for item in capabilities
        ):
            raise ConfigurationError("adapter capabilities are invalid")
        frozen_capabilities = frozenset(capabilities)
        if ProviderCapability.CHAT.value not in frozen_capabilities:
            raise ConfigurationError("provider adapters must declare chat capability")
        has_auth = ProviderCapability.AUTH.value in frozen_capabilities
        if has_auth != (self.auth is not None):
            raise ConfigurationError("auth specification and capability must appear together")
        has_models = ProviderCapability.MODELS.value in frozen_capabilities
        if has_models != (self.models is not None):
            raise ConfigurationError("model probe and capability must appear together")
        if ProviderCapability.STREAM.value in frozen_capabilities and self.transport not in {
            TransportKind.JSONL,
            TransportKind.JSON_RPC,
            TransportKind.ACP,
            TransportKind.HTTP_SSE,
        }:
            raise ConfigurationError("stream capability requires a streaming transport")
        has_permissions = ProviderCapability.PERMISSIONS.value in frozen_capabilities
        if has_permissions:
            if ProviderCapability.TOOLS.value not in frozen_capabilities:
                raise ConfigurationError("permissions capability requires tools capability")
            if type(self.permission_policy) is not PermissionPolicy:
                raise ConfigurationError("permissions capability requires a default-deny policy")
            raise ConfigurationError(
                "permissions capability is unavailable until a transport runtime binds its policy"
            )
        elif self.permission_policy is not None:
            raise ConfigurationError("permission policy requires permissions capability")
        structured_transports = {
            TransportKind.JSON,
            TransportKind.JSONL,
            TransportKind.JSON_RPC,
            TransportKind.ACP,
            TransportKind.HTTP_JSON,
            TransportKind.HTTP_SSE,
        }
        for capability in (
            ProviderCapability.SESSIONS.value,
            ProviderCapability.TOOLS.value,
            ProviderCapability.PERMISSIONS.value,
            ProviderCapability.REASONING_SUMMARIES.value,
            ProviderCapability.IMAGES.value,
            ProviderCapability.MCP.value,
        ):
            if capability in frozen_capabilities and self.transport not in structured_transports:
                raise ConfigurationError(
                    "{} capability requires a structured transport".format(capability)
                )
        if (
            ProviderCapability.MCP.value in frozen_capabilities
            and self.transport
            not in {
                TransportKind.JSON_RPC,
                TransportKind.ACP,
                TransportKind.HTTP_JSON,
                TransportKind.HTTP_SSE,
            }
        ):
            raise ConfigurationError(
                "mcp capability requires JSON-RPC, ACP, or structured HTTP transport"
            )
        if not frozen_capabilities <= self.binary.feature_probe.required_features:
            raise ConfigurationError(
                "every adapter capability requires binary feature probe evidence"
            )
        route_prefix = provider_id if self.route_prefix is None else _provider_id(self.route_prefix)
        if route_prefix != provider_id:
            raise ConfigurationError("adapter ABI v1 route prefix must equal its provider id")
        namespace = (
            provider_id
            if self.session_namespace is None
            else _provider_id(self.session_namespace)
        )
        if namespace != provider_id:
            raise ConfigurationError("adapter session namespace must equal its provider id")
        if not isinstance(self.server_policy, AdapterServerPolicy):
            raise ConfigurationError("adapter server policy must be AdapterServerPolicy")
        if (
            type(self.abi_version) is not int
            or self.abi_version != PROVIDER_ADAPTER_ABI_V1
        ):
            raise ConfigurationError("unsupported provider adapter ABI")
        object.__setattr__(self, "id", provider_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "capabilities", frozen_capabilities)
        object.__setattr__(self, "route_prefix", route_prefix)
        object.__setattr__(self, "session_namespace", namespace)
        _consume_spec_utf8_budget(self, _MAX_ADAPTER_SPEC_UTF8_BYTES)


@dataclass(frozen=True)
class AdapterDescriptorV1:
    """Safe metadata-only view suitable for passive listing."""

    id: str
    display_name: str
    status: AdapterStatus
    capabilities: FrozenSet[str]
    transport: TransportKind
    transport_config: TransportConfig
    route_prefix: str
    session_namespace: str
    server_enabled: bool
    abi_version: int = PROVIDER_ADAPTER_ABI_V1


def describe_adapter(spec: ProviderAdapterSpecV1) -> AdapterDescriptorV1:
    if not isinstance(spec, ProviderAdapterSpecV1):
        raise ConfigurationError("adapter metadata must be ProviderAdapterSpecV1")
    return AdapterDescriptorV1(
        id=spec.id,
        display_name=spec.display_name,
        status=spec.status,
        capabilities=spec.capabilities,
        transport=spec.transport,
        transport_config=spec.transport_config,
        route_prefix=spec.route_prefix,
        session_namespace=spec.session_namespace,
        server_enabled=False,
        abi_version=spec.abi_version,
    )


__all__ = [
    "PROVIDER_ADAPTER_ABI_V1",
    "AdapterDescriptorV1",
    "AdapterServerPolicy",
    "AdapterStatus",
    "AuthSpec",
    "BinarySpec",
    "BuiltPromptInvocation",
    "DeclarativeProbeSpec",
    "DoctorProbeSpec",
    "DynamicArgument",
    "EnvironmentPolicy",
    "ExitStatusProbeSpec",
    "FeatureProbeSpec",
    "FixedCommandSpec",
    "JsonProbeSpec",
    "ModelProbeSpec",
    "OperationLimits",
    "PromptCommandSpec",
    "PromptMode",
    "PromptSentinelPolicy",
    "ProbeFormat",
    "PlainTextFieldSpec",
    "PlainTextProbeSpec",
    "ProviderAdapterSpecV1",
    "ProviderCapability",
    "TransportKind",
    "TransportConfig",
    "VersionProbeSpec",
    "describe_adapter",
    "valid_provider_id",
]
