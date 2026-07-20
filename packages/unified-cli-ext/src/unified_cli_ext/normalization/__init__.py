"""Immutable normalized event and stream-state contracts."""

from .events import (
    DoneEvent,
    ErrorEvent,
    FinalTextEvent,
    FrozenJsonMap,
    NormalizedEvent,
    PermissionDecision,
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
from .normalizer import EventNormalizer
from .text import TextDeduplicator

__all__ = [
    "DoneEvent",
    "ErrorEvent",
    "EventNormalizer",
    "FinalTextEvent",
    "FrozenJsonMap",
    "NormalizedEvent",
    "PermissionDecision",
    "PermissionRequestEvent",
    "ReasoningSummaryEvent",
    "SessionEvent",
    "SessionRef",
    "TextDeduplicator",
    "TextDeltaEvent",
    "ToolProgressEvent",
    "ToolResultEvent",
    "ToolStartEvent",
    "UsageEvent",
    "freeze_json",
]
