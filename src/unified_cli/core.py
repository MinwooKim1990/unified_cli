"""Core data types shared across providers."""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional, Union


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


# ---- image attachments ----

@dataclass
class Attachment:
    """An image input — exactly one of `path`, `bytes_`, or `url` is set.

    Use `normalize_image()` to coerce arbitrary user input (Path, str path,
    bytes, str URL, or existing Attachment) into this shape.
    """
    path: Optional[str] = None        # local filesystem path (preferred for CLIs)
    bytes_: Optional[bytes] = None    # raw image bytes
    url: Optional[str] = None         # remote URL (Anthropic + OpenAI accept)
    media_type: Optional[str] = None  # "image/png" etc; auto-detected if None

    @property
    def is_path(self) -> bool: return self.path is not None
    @property
    def is_bytes(self) -> bool: return self.bytes_ is not None
    @property
    def is_url(self) -> bool: return self.url is not None


# Common image MIME map (covers all current provider-supported formats).
_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".heic": "image/heic", ".heif": "image/heif",
}


def _detect_media_type(path: str) -> Optional[str]:
    p = Path(path)
    ext = p.suffix.lower()
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    guess, _ = mimetypes.guess_type(path)
    return guess if (guess and guess.startswith("image/")) else None


ImageInput = Union[str, "Path", bytes, Attachment]


def normalize_image(item) -> Attachment:
    """Coerce any supported input form into an `Attachment`.

    Accepts:
      - `Attachment` (returned as-is, with media_type filled in if missing)
      - `pathlib.Path` or `str` that points to a local file → path attachment
      - `str` starting with http(s):// → url attachment
      - `bytes` → bytes attachment (caller can set media_type later)
    """
    if isinstance(item, Attachment):
        if item.media_type is None and item.path:
            item.media_type = _detect_media_type(item.path)
        return item
    if isinstance(item, Path):
        return Attachment(path=str(item), media_type=_detect_media_type(str(item)))
    if isinstance(item, (bytes, bytearray)):
        return Attachment(bytes_=bytes(item))
    if isinstance(item, str):
        if item.startswith(("http://", "https://", "data:")):
            return Attachment(url=item)
        # Treat as path
        return Attachment(path=item, media_type=_detect_media_type(item))
    raise TypeError(
        f"Unsupported image input type: {type(item).__name__}. "
        "Use str/Path/bytes/Attachment."
    )


def normalize_images(items) -> list[Attachment]:
    """Normalize a list of mixed inputs. None / [] returns []."""
    if not items:
        return []
    return [normalize_image(x) for x in items]


def attachment_bytes(att: Attachment) -> bytes:
    """Read attachment payload as bytes (path → file read, bytes → as-is).

    URL attachments are not auto-fetched (caller must download first); raises
    `ValueError` if asked.
    """
    if att.is_bytes:
        return att.bytes_  # type: ignore[return-value]
    if att.is_path:
        return Path(att.path).read_bytes()  # type: ignore[arg-type]
    raise ValueError(
        "URL attachments must be fetched by the caller; only path/bytes "
        "can be read locally."
    )


def attachment_b64(att: Attachment) -> str:
    """Return base64-encoded payload (used by Anthropic Messages format)."""
    return base64.b64encode(attachment_bytes(att)).decode("ascii")
