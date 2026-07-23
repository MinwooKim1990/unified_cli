"""Immutable normalized events shared by extension adapters."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterator, Optional, Tuple, Union

from ..errors import ProtocolError
from .validation import utf8_size, validate_unicode


_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MAX_SESSION_BYTES = 1024
_SENSITIVE_REASONING_KEYS = frozenset(
    {
        "chainofthought",
        "rawreasoning",
        "reasoning",
        "reasoningcontent",
        "thinking",
        "thinkingcontent",
    }
)

FrozenScalar = Union[None, bool, int, float, str]
_FROZEN_JSON_TOKEN = object()


class FrozenJsonMap(Mapping):
    """Small immutable mapping used by frozen event payloads."""

    __slots__ = ("_items", "_dict")

    def __init__(
        self,
        items: Iterator[Tuple[str, Any]],
        *,
        _token: object = None,
    ) -> None:
        if _token is not _FROZEN_JSON_TOKEN:
            raise TypeError("FrozenJsonMap values must be created with freeze_json()")
        collected = []
        for index, pair in enumerate(items):
            if index >= 10_000:
                raise ProtocolError("JSON object contains too many entries")
            collected.append(pair)
        pairs = tuple(collected)
        keys = tuple(key for key, _ in pairs)
        if len(keys) != len(set(keys)):
            raise ProtocolError("JSON object contains duplicate keys")
        self._items = pairs
        self._dict = MappingProxyType(dict(pairs))

    def __getitem__(self, key: str) -> Any:
        return self._dict[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._dict)

    def __len__(self) -> int:
        return len(self._dict)

    def __repr__(self) -> str:
        return "FrozenJsonMap({!r})".format(self._dict)


class _FreezeBudget:
    __slots__ = ("nodes", "bytes")

    def __init__(self) -> None:
        self.nodes = 0
        self.bytes = 0

    def consume(self) -> None:
        self.nodes += 1
        if self.nodes > 100_000:
            raise ProtocolError("JSON payload contains too many values")

    def consume_bytes(self, size: int) -> None:
        self.bytes += size
        if self.bytes > 16 * 1024 * 1024:
            raise ProtocolError("JSON payload exceeds its aggregate size limit")


def _freeze(value: Any, budget: _FreezeBudget, depth: int, drop_reasoning: bool) -> Any:
    budget.consume()
    if depth > 32:
        raise ProtocolError("JSON nesting exceeds 32 levels")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > 2**63 - 1:
            raise ProtocolError("JSON integer exceeds signed 64-bit bounds")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ProtocolError("JSON number must be finite")
        return value
    if isinstance(value, str):
        validate_unicode(
            value,
            label="JSON string",
            maximum=1024 * 1024,
            empty=True,
            allow_text_newlines=True,
        )
        budget.consume_bytes(utf8_size(value, label="JSON string"))
        return value
    if isinstance(value, Mapping):
        clean = []
        seen = set()
        try:
            iterator = iter(value.items())
            for index, pair in enumerate(iterator):
                if index >= 10_000:
                    raise ProtocolError("JSON object contains too many entries")
                try:
                    key, item = pair
                except (TypeError, ValueError):
                    raise ProtocolError("JSON object iterator is malformed") from None
                validate_unicode(key, label="JSON object key", maximum=1024, empty=False)
                budget.consume_bytes(utf8_size(key, label="JSON object key"))
                if key in seen:
                    raise ProtocolError("JSON object contains duplicate keys")
                seen.add(key)
                normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
                if drop_reasoning and normalized_key in _SENSITIVE_REASONING_KEYS:
                    continue
                clean.append((key, _freeze(item, budget, depth + 1, drop_reasoning)))
        except ProtocolError:
            raise
        except Exception:
            raise ProtocolError("JSON object iteration failed") from None
        return FrozenJsonMap(iter(clean), _token=_FROZEN_JSON_TOKEN)
    if type(value) in (list, tuple):
        if len(value) > 10_000:
            raise ProtocolError("JSON sequence contains too many entries")
        return tuple(_freeze(item, budget, depth + 1, drop_reasoning) for item in value)
    raise ProtocolError("event payload must contain JSON values only")


def freeze_json(value: Any, *, drop_reasoning: bool = True) -> Any:
    """Deep-freeze bounded JSON input while dropping raw reasoning fields."""

    return _freeze(value, _FreezeBudget(), 0, drop_reasoning)


@dataclass(frozen=True)
class SessionRef:
    """Provider-namespaced opaque session reference."""

    provider: str
    session_id: str

    def __post_init__(self) -> None:
        if type(self.provider) is not str or not _PROVIDER_RE.fullmatch(self.provider):
            raise ProtocolError("invalid provider namespace")
        validate_unicode(
            self.session_id,
            label="provider session id",
            maximum=_MAX_SESSION_BYTES,
            empty=False,
        )

    @property
    def namespaced(self) -> str:
        return "{}:{}".format(self.provider, self.session_id)

    @classmethod
    def parse(cls, value: str) -> "SessionRef":
        validate_unicode(
            value,
            label="namespaced session reference",
            maximum=_MAX_SESSION_BYTES + 65,
            empty=False,
        )
        provider, separator, session_id = value.partition(":")
        if not separator:
            raise ProtocolError("session reference must include a provider namespace")
        return cls(provider=provider, session_id=session_id)


class PermissionDecision(str, Enum):
    DENY = "deny"
    ALLOW_ONCE = "allow_once"


@dataclass(frozen=True)
class SessionEvent:
    session: SessionRef

    def __post_init__(self) -> None:
        if not isinstance(self.session, SessionRef):
            raise ProtocolError("session event requires a SessionRef")


@dataclass(frozen=True)
class TextDeltaEvent:
    text: str
    block_id: str = "default"

    def __post_init__(self) -> None:
        validate_unicode(self.text, label="text delta", maximum=1024 * 1024, empty=True, allow_text_newlines=True)
        validate_unicode(self.block_id, label="text block id", maximum=256, empty=False)


@dataclass(frozen=True)
class FinalTextEvent:
    """Final text with only the unseen suffix in ``text``.

    ``complete_text`` is authoritative and may repeat prior deltas; consumers
    that render incrementally append only ``text``.
    """

    text: str
    complete_text: str

    def __post_init__(self) -> None:
        validate_unicode(self.text, label="final text delta", maximum=1024 * 1024, empty=True, allow_text_newlines=True)
        validate_unicode(self.complete_text, label="complete final text", maximum=16 * 1024 * 1024, empty=True, allow_text_newlines=True)


@dataclass(frozen=True)
class ReasoningSummaryEvent:
    summary: str

    def __post_init__(self) -> None:
        validate_unicode(self.summary, label="reasoning summary", maximum=1024 * 1024, empty=True, allow_text_newlines=True)


@dataclass(frozen=True)
class ToolStartEvent:
    tool_id: str
    name: str
    arguments: Mapping = field(default_factory=lambda: freeze_json({}))

    def __post_init__(self) -> None:
        validate_unicode(self.tool_id, label="tool id", maximum=256, empty=False)
        validate_unicode(self.name, label="tool name", maximum=256, empty=False)
        object.__setattr__(self, "arguments", freeze_json(self.arguments))


@dataclass(frozen=True)
class ToolProgressEvent:
    tool_id: str
    message: str
    progress: Optional[float] = None

    def __post_init__(self) -> None:
        validate_unicode(self.tool_id, label="tool id", maximum=256, empty=False)
        validate_unicode(self.message, label="tool progress", maximum=1024 * 1024, empty=True, allow_text_newlines=True)
        if self.progress is not None:
            if isinstance(self.progress, bool) or type(self.progress) not in (int, float):
                raise ProtocolError("tool progress must be between zero and one")
            try:
                numeric = float(self.progress)
            except (OverflowError, ValueError):
                raise ProtocolError("tool progress must be between zero and one") from None
            if not math.isfinite(numeric) or not 0 <= numeric <= 1:
                raise ProtocolError("tool progress must be between zero and one")
            object.__setattr__(self, "progress", numeric)


@dataclass(frozen=True)
class ToolResultEvent:
    tool_id: str
    result: Any = None
    is_error: bool = False

    def __post_init__(self) -> None:
        validate_unicode(self.tool_id, label="tool id", maximum=256, empty=False)
        if type(self.is_error) is not bool:
            raise ProtocolError("tool result is_error must be a boolean")
        object.__setattr__(self, "result", freeze_json(self.result))


@dataclass(frozen=True)
class PermissionRequestEvent:
    request_id: str
    operation: str
    tool_id: Optional[str] = None
    details: Mapping = field(default_factory=lambda: freeze_json({}))

    def __post_init__(self) -> None:
        validate_unicode(self.request_id, label="permission request id", maximum=256, empty=False)
        validate_unicode(self.operation, label="permission operation", maximum=256, empty=False)
        if self.tool_id is not None:
            validate_unicode(self.tool_id, label="tool id", maximum=256, empty=False)
        object.__setattr__(self, "details", freeze_json(self.details))


@dataclass(frozen=True)
class UsageEvent:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    def __post_init__(self) -> None:
        for value in (self.input_tokens, self.output_tokens, self.cached_input_tokens):
            if type(value) is not int or value < 0 or value > 10**15:
                raise ProtocolError("usage counters must be bounded nonnegative integers")


@dataclass(frozen=True)
class ErrorEvent:
    code: str
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        validate_unicode(self.code, label="error code", maximum=128, empty=False)
        validate_unicode(self.message, label="error message", maximum=4096, empty=True, allow_text_newlines=True)
        if type(self.retryable) is not bool:
            raise ProtocolError("error retryable must be a boolean")


@dataclass(frozen=True)
class DoneEvent:
    reason: str = "complete"

    def __post_init__(self) -> None:
        validate_unicode(self.reason, label="done reason", maximum=128, empty=True)


NormalizedEvent = Union[
    SessionEvent,
    TextDeltaEvent,
    FinalTextEvent,
    ReasoningSummaryEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolResultEvent,
    PermissionRequestEvent,
    UsageEvent,
    ErrorEvent,
    DoneEvent,
]
