"""Core data types shared across providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


ProviderName = Literal["claude", "codex", "gemini"]
MessageKind = Literal[
    "text",          # assistant text chunk or complete text
    "reasoning",     # provider's thinking (Codex reasoning, Claude thinking)
    "tool_use",      # tool invocation: tool={"name","input","id"}
    "tool_result",   # tool output:    tool={"id","output","is_error"}
    "session",       # carries session_id (emitted at start or end)
    "usage",         # carries usage stats (emitted at end)
    "done",          # turn complete
    "error",         # provider-level error event (wrapper may raise)
]


@dataclass
class Usage:
    """Token usage for a single turn. All fields optional."""

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


@dataclass
class Message:
    """Normalized streaming event — one of these per provider output item."""

    kind: MessageKind
    provider: ProviderName
    text: Optional[str] = None
    tool: Optional[dict] = None
    session_id: Optional[str] = None
    usage: Optional[Usage] = None
    error: Optional[str] = None
    raw: dict = field(default_factory=dict)


@dataclass
class Response:
    """Aggregated single-turn result."""

    text: str
    session_id: str
    provider: ProviderName
    model: str
    usage: Usage
    messages: list[Message]
    raw: list[dict]


@dataclass
class ModelInfo:
    """One entry in `list_models()`."""

    id: str
    provider: ProviderName
    display_name: str = ""
    default: bool = False
    deprecated: bool = False
    source: Literal["api", "cache", "hardcoded"] = "hardcoded"
