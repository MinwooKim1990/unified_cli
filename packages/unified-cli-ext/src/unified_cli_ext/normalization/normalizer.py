"""Canonical, fail-closed conversion from JSON objects to immutable events."""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any, List, Optional

from ..errors import ProtocolError
from .events import (
    DoneEvent,
    ErrorEvent,
    FinalTextEvent,
    NormalizedEvent,
    PermissionRequestEvent,
    ReasoningSummaryEvent,
    SessionEvent,
    SessionRef,
    TextDeltaEvent,
    ToolProgressEvent,
    ToolResultEvent,
    ToolStartEvent,
    UsageEvent,
    freeze_json,
)
from .text import TextDeduplicator
from .validation import validate_unicode
from ..tools import ToolCorrelator, validate_correlation_id


_RAW_REASONING_TYPES = frozenset({"reasoning", "thinking", "chain_of_thought", "raw_reasoning"})


def _string(raw: Mapping, key: str, *, empty: bool = False, maximum: int = 1 << 20) -> str:
    value = raw.get(key)
    return validate_unicode(
        value,
        label=key,
        maximum=maximum,
        empty=empty,
        allow_text_newlines=key in {"text", "summary", "message"},
    )


def _nonnegative_int(raw: Mapping, key: str) -> int:
    value = raw.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 10**15:
        raise ProtocolError("{} must be a bounded nonnegative integer".format(key))
    return value


class EventNormalizer:
    """Stateful normalizer for the adapter-neutral Ext event schema.

    Unknown events fail closed. Raw reasoning event kinds are deliberately
    discarded and never copied into state or normalized payloads.
    """

    def __init__(self, provider: str, *, max_text_bytes: int = 16 * 1024 * 1024) -> None:
        # Validate provider namespace without inventing a session value.
        SessionRef(provider=provider, session_id="probe")
        self.provider = provider
        self._text = TextDeduplicator(max_text_bytes=max_text_bytes)
        self._tools = ToolCorrelator()
        self._session: Optional[SessionRef] = None

    @property
    def session(self) -> Optional[SessionRef]:
        return self._session

    def feed(self, raw: Mapping) -> List[NormalizedEvent]:
        if not isinstance(raw, Mapping):
            raise ProtocolError("normalized input must be a JSON object")
        try:
            canonical = freeze_json(raw, drop_reasoning=False)
        except ProtocolError:
            raise ProtocolError("normalized input is not bounded JSON") from None
        except Exception:
            raise ProtocolError("normalized input is malformed") from None
        if not isinstance(canonical, Mapping):
            raise ProtocolError("normalized input must be a JSON object")
        raw = canonical
        event_type = raw.get("type")
        if type(event_type) is not str:
            raise ProtocolError("event type must be a string")
        if event_type in _RAW_REASONING_TYPES:
            return []
        if event_type == "session":
            session = SessionRef(self.provider, _string(raw, "session_id", maximum=1024))
            if self._session is not None and self._session != session:
                raise ProtocolError("provider changed session id mid-stream")
            self._session = session
            return [SessionEvent(session)]
        if event_type == "text_delta":
            block = raw.get("block_id", "default")
            text = self._text.delta(_string(raw, "text", empty=True), block)
            return [TextDeltaEvent(text, block)] if text else []
        if event_type == "text_partial":
            block = raw.get("block_id", "default")
            text = self._text.partial(_string(raw, "text", empty=True), block)
            return [TextDeltaEvent(text, block)] if text else []
        if event_type == "text_final":
            unseen, complete = self._text.final(_string(raw, "text", empty=True))
            return [FinalTextEvent(unseen, complete)]
        if event_type == "reasoning_summary":
            return [ReasoningSummaryEvent(_string(raw, "summary", empty=True))]
        if event_type == "tool_start":
            tool_id = _string(raw, "tool_id", maximum=256)
            name = _string(raw, "name", maximum=256)
            arguments = raw.get("arguments", {})
            if not isinstance(arguments, Mapping):
                raise ProtocolError("tool arguments must be an object")
            event = ToolStartEvent(tool_id, name, arguments)
            self._tools.start(tool_id, name)
            return [event]
        if event_type == "tool_progress":
            tool_id = _string(raw, "tool_id", maximum=256)
            progress = raw.get("progress")
            if progress is not None and (
                isinstance(progress, bool)
                or type(progress) not in (int, float)
                or not math.isfinite(float(progress))
                or not 0 <= float(progress) <= 1
            ):
                raise ProtocolError("progress must be between zero and one")
            event = ToolProgressEvent(
                tool_id,
                _string(raw, "message", empty=True),
                None if progress is None else float(progress),
            )
            self._tools.progress(tool_id)
            return [event]
        if event_type == "tool_result":
            tool_id = _string(raw, "tool_id", maximum=256)
            is_error = raw.get("is_error", False)
            if type(is_error) is not bool:
                raise ProtocolError("is_error must be a boolean")
            event = ToolResultEvent(tool_id, raw.get("result"), is_error)
            self._tools.result(tool_id)
            return [event]
        if event_type == "permission":
            request_id = _string(raw, "request_id", maximum=256)
            validate_correlation_id(request_id, label="permission request id")
            tool_id = raw.get("tool_id")
            if tool_id is not None:
                validate_correlation_id(tool_id, label="tool id")
            details = raw.get("details", {})
            if not isinstance(details, Mapping):
                raise ProtocolError("permission details must be an object")
            return [
                PermissionRequestEvent(
                    request_id,
                    _string(raw, "operation", maximum=256),
                    tool_id,
                    details,
                )
            ]
        if event_type == "usage":
            return [
                UsageEvent(
                    _nonnegative_int(raw, "input_tokens"),
                    _nonnegative_int(raw, "output_tokens"),
                    _nonnegative_int(raw, "cached_input_tokens"),
                )
            ]
        if event_type == "error":
            retryable = raw.get("retryable", False)
            if type(retryable) is not bool:
                raise ProtocolError("retryable must be a boolean")
            return [
                ErrorEvent(
                    _string(raw, "code", maximum=128),
                    _string(raw, "message", empty=True, maximum=4096),
                    retryable,
                )
            ]
        if event_type == "done":
            return [DoneEvent(_string(raw, "reason", empty=True, maximum=128))]
        raise ProtocolError("unknown normalized event type")


__all__ = ["EventNormalizer"]
