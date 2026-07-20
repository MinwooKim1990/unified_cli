"""Fail-closed correlation for tool lifecycle events."""

from __future__ import annotations

from typing import Dict

from ..errors import LimitExceeded, ProtocolError
from ..normalization.validation import validate_unicode


def validate_correlation_id(value: str, *, label: str = "correlation id") -> str:
    return validate_unicode(value, label=label, maximum=256, empty=False)


class ToolCorrelator:
    def __init__(self, max_active: int = 1024) -> None:
        if type(max_active) is not int or max_active <= 0:
            raise ValueError("max_active must be a positive integer")
        self._max_active = max_active
        self._active: Dict[str, str] = {}

    @property
    def active_count(self) -> int:
        return len(self._active)

    def start(self, tool_id: str, name: str) -> None:
        validate_correlation_id(tool_id, label="tool id")
        validate_unicode(name, label="tool name", maximum=256, empty=False)
        if tool_id in self._active:
            raise ProtocolError("duplicate active tool id")
        if len(self._active) >= self._max_active:
            raise LimitExceeded("too many active tool calls")
        self._active[tool_id] = name

    def progress(self, tool_id: str) -> None:
        validate_correlation_id(tool_id, label="tool id")
        if tool_id not in self._active:
            raise ProtocolError("unmatched tool progress")

    def result(self, tool_id: str) -> None:
        validate_correlation_id(tool_id, label="tool id")
        if tool_id not in self._active:
            raise ProtocolError("unmatched tool result")
        del self._active[tool_id]


__all__ = ["ToolCorrelator", "validate_correlation_id"]
